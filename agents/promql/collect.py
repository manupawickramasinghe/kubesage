"""PromQL sub-agent: per-workload CPU, throttling, threads, memory, OOM kills, disk I/O and latency."""
import re

_REPLICA = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[a-z0-9]{5}$")


def workload(pod):
    return _REPLICA.sub("", pod)


def collect(ctx):
    ns = "|".join(ctx.namespaces)
    w = ctx.settings.get("window", "2m")
    sel = f'namespace=~"{ns}", container!=""'

    def per(expr, keys=("namespace", "pod", "container")):
        """{(namespace, workload, container): [values across pods]} for one instant query."""
        out = {}
        for labels, value in ctx.query(expr):
            key = (labels.get("namespace", ""), workload(labels.get("pod", "")), labels.get("container", ""))
            out.setdefault(key if "container" in keys else key[:2], []).append(value)
        return out

    by = "namespace, pod, container"
    cpu = per(f"sum by ({by}) (rate(container_cpu_usage_seconds_total{{{sel}}}[{w}]))")
    cpu_min = per(f"min_over_time(sum by ({by}) (rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))[5m:30s])")
    cpu_max = per(f"max_over_time(sum by ({by}) (rate(container_cpu_usage_seconds_total{{{sel}}}[1m]))[5m:30s])")
    req_cpu = per(f'max by ({by}) (kube_pod_container_resource_requests{{namespace=~"{ns}", resource="cpu"}})')
    lim_cpu = per(f'max by ({by}) (kube_pod_container_resource_limits{{namespace=~"{ns}", resource="cpu"}})')
    throttled = per(f"sum by ({by}) (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[{w}]))"
                    f" / sum by ({by}) (rate(container_cpu_cfs_periods_total{{{sel}}}[{w}]))")
    threads = per(f"max by ({by}) (container_threads{{{sel}}})")
    mem = per(f"max by ({by}) (container_memory_working_set_bytes{{{sel}}})")
    lim_mem = per(f'max by ({by}) (kube_pod_container_resource_limits{{namespace=~"{ns}", resource="memory"}})')
    oom = per(f"increase(container_oom_events_total{{{sel}}}[10m])")
    restarts = per(f'increase(kube_pod_container_status_restarts_total{{namespace=~"{ns}"}}[10m])')
    cpu_stall = per(f"rate(container_pressure_cpu_stalled_seconds_total{{{sel}}}[{w}])")
    io_stall = per(f"rate(container_pressure_io_stalled_seconds_total{{{sel}}}[{w}])")
    writes = per(f'sum by (namespace, pod) (rate(container_fs_writes_bytes_total{{namespace=~"{ns}", container!=""}}[1m]))',
                 keys=("namespace", "pod"))

    for key in sorted(set(cpu) | set(mem)):
        namespace, name, container = key
        target = f"deployment/{namespace}/{name}"
        pods = len(cpu.get(key) or mem.get(key) or [])
        used = max(cpu.get(key, [0]))
        lim = max(lim_cpu.get(key, [0])) or None
        req = max(req_cpu.get(key, [0])) or None
        thr = max(throttled.get(key, [0]))
        th = max(threads.get(key, [0])) or None
        detail = f"{pods} pod(s); request {req or '-'} / limit {lim or 'none'} cores"
        ctx.signal(f"{name}.cpu", "cpu_cores", target, used, unit="cores", detail=detail,
                   severity="warning" if lim and used > 0.85 * lim else None,
                   source=f"rate(container_cpu_usage_seconds_total[{w}])")
        if lim:
            ctx.signal(f"{name}.cpu_limit", "cpu_limit", target, lim, unit="cores", detail=f"request {req} cores")
        ctx.signal(f"{name}.throttled", "cpu_throttled_ratio", target, thr, unit="ratio",
                   detail="share of CFS periods in which the container was throttled",
                   severity="warning" if thr > 0.25 else None)
        if th:
            ctx.signal(f"{name}.threads", "threads", target, th, unit="threads")
        lo, hi = min(cpu_min.get(key, [used])), max(cpu_max.get(key, [used]))
        if used >= 0.5 and hi - lo < 0.06 * max(hi, 0.01):
            ctx.signal(f"{name}.cpu_flat", "cpu_flat", target, round(used, 2), unit="cores",
                       detail=f"1m CPU rate stayed between {lo:.2f} and {hi:.2f} cores for 5 minutes"
                              f"{f'; limit {lim} cores, {thr:.0%} throttled, {th:g} threads' if lim else ''}",
                       severity="warning" if lim and used < 0.6 * lim and thr < 0.05 else None)
        m, ml = max(mem.get(key, [0])), max(lim_mem.get(key, [0])) or None
        ctx.signal(f"{name}.memory", "memory_working_set", target, round(m / 2 ** 20), unit="MiB",
                   detail=f"limit {round(ml / 2 ** 20) if ml else 'none'} MiB",
                   severity="warning" if ml and m > 0.85 * ml else None)
        kills = sum(oom.get(key, [0]))
        if kills:
            ctx.signal(f"{name}.oom", "oom_kills_10m", target, round(kills), unit="kills", severity="critical")
        r = sum(restarts.get(key, [0]))
        if r >= 1:
            ctx.signal(f"{name}.restarts", "restarts_10m", target, round(r), unit="restarts", severity="warning")
        for name_, series, sev in (("cpu_stall", cpu_stall, 0.2), ("io_stall", io_stall, 0.05)):
            v = max(series.get(key, [0]))
            if v > 0.01:
                ctx.signal(f"{name}.{name_}", f"pressure_{name_}", target, v, unit="s/s",
                           detail="share of time some task stalled waiting (PSI)", severity="warning" if v > sev else None)
    for (namespace, name), values in sorted(writes.items()):
        rate = sum(values)
        if rate > 1e5:
            ctx.signal(f"{name}.disk_writes", "disk_write_rate", f"deployment/{namespace}/{name}", round(rate / 1e6, 2),
                       unit="MB/s", severity="warning" if rate > 10e6 else None,
                       source="rate(container_fs_writes_bytes_total[1m])")
    for labels, p95 in ctx.query(f'histogram_quantile(0.95, sum by (le, namespace, app) '
                                 f'(rate(http_request_duration_seconds_bucket{{namespace=~"{ns}"}}[1m])))'):
        if p95 == p95:  # NaN when there was no traffic
            ctx.signal(f"{labels.get('app')}.p95_server", "latency_p95_server", f"deployment/{labels.get('namespace')}/{labels.get('app')}",
                       round(p95 * 1000), unit="ms", detail="measured inside the app, from when a thread picks the request up",
                       severity="warning" if p95 > 0.5 else None)
    for labels, p95 in ctx.query(f'histogram_quantile(0.95, sum by (le, namespace, target) '
                                 f'(rate(probe_http_duration_seconds_bucket{{namespace=~"{ns}"}}[1m])))'):
        if p95 == p95:
            ctx.signal(f"{labels.get('target')}.p95_client", "latency_p95_client", f"deployment/{labels.get('namespace')}/{labels.get('target')}",
                       round(p95 * 1000), unit="ms", detail="what callers wait, queueing included (in-cluster probe)",
                       severity="warning" if p95 > 0.5 else None)
    for labels, rate in ctx.query(f'sum by (namespace, target) (rate(probe_http_failures_total{{namespace=~"{ns}"}}[1m]))'):
        if rate > 0:
            ctx.signal(f"{labels.get('target')}.probe_failures", "probe_failures", f"deployment/{labels.get('namespace')}/{labels.get('target')}",
                       round(rate, 2), unit="req/s", detail="probe requests that failed or timed out (30 s)", severity="warning")
    for labels, rps in ctx.query(f'sum by (namespace, app) (rate(http_request_duration_seconds_count{{namespace=~"{ns}"}}[1m]))'):
        ctx.signal(f"{labels.get('app')}.rps", "request_rate", f"deployment/{labels.get('namespace')}/{labels.get('app')}",
                   rps, unit="req/s")
    ctx.log(f"{len(ctx.signals)} signals from {len(ctx.prom.calls)} PromQL queries")
