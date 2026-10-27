"""Analysis reports appear only when all their manifests are ready and never overwrite a prior run."""
from pathlib import Path

import pytest

from kubesage import apply
from kubesage.pipeline import write_run


def test_published_run_is_immutable(result, tmp_path):
    run = write_run(result, tmp_path / "20260101-000000")
    original = (run / "report.json").read_text()
    with pytest.raises(FileExistsError):
        write_run(result, run)
    assert (run / "report.json").read_text() == original


def test_failed_write_leaves_the_previous_run_available(result, tmp_path, monkeypatch):
    previous = write_run(result, tmp_path / "20260101-000000")
    target = tmp_path / "20260102-000000"
    write_text = Path.write_text

    def fail_report(path, text, *args, **kwargs):
        if path.name == "report.json":
            assert not target.exists()
            assert apply.latest_run(tmp_path) == previous
            raise OSError("disk full")
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_report)
    with pytest.raises(OSError, match="disk full"):
        write_run(result, target)
    assert apply.latest_run(tmp_path) == previous
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [previous]  # incomplete staging files were removed
