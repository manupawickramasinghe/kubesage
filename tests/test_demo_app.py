"""The in-cluster demo app (deploy/k8s/demo/app.py) serves and burns CPU per request."""
import importlib.util
import threading
import time
import urllib.request
from pathlib import Path

from kubesage.live import APP_IMAGE, spike_job

APP = Path(__file__).resolve().parent.parent / "deploy/k8s/demo/app.py"


def _load_app():
    spec = importlib.util.spec_from_file_location("demo_app", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_work_endpoint_burns_cpu():
    app = _load_app()
    server = app.serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert urllib.request.urlopen(f"{base}/healthz").read() == b"ok\n"
        started = time.monotonic()
        urllib.request.urlopen(f"{base}/work?ms=60").read()
        assert time.monotonic() - started >= 0.055
    finally:
        server.shutdown()


def test_spike_job_uses_the_demo_app_image():
    job = spike_job(namespace="demo", target="checkout-web", rps=40, work_ms=50, seconds=60)
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert container["image"] == APP_IMAGE
    assert {"name": "TARGET", "value": "http://checkout-web:8080"} in container["env"]
    assert APP_IMAGE in (APP.parent / "workloads.yaml").read_text()
