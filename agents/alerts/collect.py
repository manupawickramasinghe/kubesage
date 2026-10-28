"""Alertmanager sub-agent: the alerts firing now."""
from datetime import datetime, timezone


def collect(ctx):
    alerts = ctx.http_json(f"{ctx.alertmanager}/api/v2/alerts?active=true&silenced=false&inhibited=false")
    if alerts is None:
        ctx.signal("unreachable", "alertmanager_down", "service/alertmanager", ctx.alertmanager,
                   detail="Alertmanager did not answer; alerts are unknown", severity="warning")
        return
    now = datetime.now(timezone.utc)
    namespaces = set(ctx.namespaces)
    for a in alerts:
        labels = a.get("labels", {})
        if labels.get("namespace") and labels["namespace"] not in namespaces:
            continue
        if labels.get("pod"):
            target = f"pod/{labels.get('namespace')}/{labels['pod']}"
        elif labels.get("app"):
            target = f"deployment/{labels.get('namespace')}/{labels['app']}"
        elif labels.get("node"):
            target = f"node/{labels['node']}"
        else:
            target = f"namespace/{labels.get('namespace', '-')}"
        try:
            since = (now - datetime.fromisoformat(a["startsAt"].replace("Z", "+00:00"))).total_seconds()
        except (KeyError, ValueError):
            since = None
        ctx.signal(f"{labels.get('alertname')}.{a.get('fingerprint', '')[:8]}", labels.get("alertname", "alert"), target,
                   "firing", detail=a.get("annotations", {}).get("summary", ""),
                   severity="critical" if labels.get("severity") == "critical" else "warning",
                   source=f"firing for {round(since)} s" if since is not None else "")
    ctx.log(f"{len(ctx.signals)} alerts firing")
