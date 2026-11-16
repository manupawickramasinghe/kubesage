"""The commands that put the what-if, calendar and realized-savings modules in an operator's hands."""
import json

import pytest
import yaml

from kubesage import cli, config, service
from kubesage.pipeline import write_run


@pytest.fixture
def measured(stats, monkeypatch):
    """The cluster as the demo scenarios: no database."""
    monkeypatch.setattr(service, "collect_stats", lambda **kw: (list(stats.values()), 1000))
    return stats


def test_whatif_prices_a_bigger_rush(measured, capsys):
    cli.main(["whatif", "--multiplier", "5", "--event", "Black Friday sale", "--json"])
    rows = {r["workload"]: r for r in json.loads(capsys.readouterr().out)}
    web = rows["demo/checkout-web"]
    assert web["multiplier"] == 5 and web["replicas_needed"] > web["replicas_now"]
    assert web["monthly_scheduled_usd"] < web["monthly_at_peak_usd"] and web["scheduled_savings_pct"] > 0
    assert "demo/report-worker" not in rows  # the sale doesn't name it
    cli.main(["whatif", "--multiplier", "2"])
    out = capsys.readouterr().out
    assert "What if traffic were 2× normal?" in out and "Friday" in out and "note:" in out
    with pytest.raises(SystemExit, match="no event named 'Cyber Monday'"):
        cli.main(["whatif", "--multiplier", "2", "--event", "Cyber Monday"])


@pytest.fixture
def calendar_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    path = tmp_path / "calendar.yaml"
    path.write_text("events: []\n")
    return path


def test_calendar_suggest_drafts_and_only_writes_what_is_approved(measured, calendar_file, capsys):
    cli.main(["calendar", "suggest"])
    out = capsys.readouterr().out
    drafts = yaml.safe_load(out[:out.index("draft event(s)")].rsplit("\n", 1)[0])["events"]
    names = [e["name"] for e in drafts]
    assert names and all(n.startswith("Suggested: ") for n in names)
    assert yaml.safe_load(calendar_file.read_text()) == {"events": []}  # a draft changes nothing
    with pytest.raises(SystemExit, match="no drafted event named: nope"):
        cli.main(["calendar", "suggest", "--approve", "nope"])
    cli.main(["calendar", "suggest", "--approve", names[0]])
    assert [e.name for e in config.load_calendar().events] == [names[0]]
    capsys.readouterr()
    cli.main(["calendar", "suggest"])  # the approved window now covers that peak: it is not suggested again
    assert names[0] not in capsys.readouterr().out


def test_calendar_import_reads_an_ics_file(calendar_file, tmp_path, capsys):
    ics = tmp_path / "shop.ics"
    ics.write_text("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Spring sale\nDTSTART:20270315T090000\nDTEND:20270315T180000\n"
                   "END:VEVENT\nBEGIN:VEVENT\nSUMMARY:No dates\nEND:VEVENT\nEND:VCALENDAR\n")
    cli.main(["calendar", "import", str(ics), "--multiplier", "2.5"])
    out = capsys.readouterr().out
    assert "Spring sale" in out and "traffic_multiplier: 2.5" in out and "skipped" in out
    assert config.load_calendar().events == []
    cli.main(["calendar", "import", str(ics), "--approve", "all"])
    (event,) = config.load_calendar().events
    assert (event.name, event.date, event.start_time, event.end_time) == ("Spring sale", "2027-03-15", "09:00", "18:00")
    with pytest.raises(SystemExit, match="already has an event named 'Spring sale'"):
        cli.main(["calendar", "import", str(ics), "--approve", "all"])
    with pytest.raises(SystemExit, match="usage: kubesage calendar import"):
        cli.main(["calendar", "import"])
    with pytest.raises(SystemExit, match="cannot read"):
        cli.main(["calendar", "import", str(tmp_path / "missing.ics")])


def test_realized_compares_a_run_with_the_cluster_now(stats, result, tmp_path, monkeypatch, capsys):
    run = write_run(result, tmp_path / "out" / "20260101-000000")
    # Since that run, payment-service was right-sized to a tenth of its CPU request; nothing else changed.
    now = [s.model_copy(update={"cpu_request_cores": s.cpu_request_cores / 10}) if s.name == "payment-service" else s
           for s in stats.values()]
    monkeypatch.setattr(service, "collect_stats", lambda **kw: (now, 1000))
    cli.main(["realized", "--out-dir", str(tmp_path / "out"), "--json"])
    report = json.loads(capsys.readouterr().out)
    rows = {r["workload"]: r for r in report["rows"]}
    assert rows["demo/payment-service"]["realized_savings_pct"] > 0
    assert rows["demo/payment-service"]["predicted_savings_pct"] is not None
    assert rows["demo/catalog-api"]["realized_savings_pct"] == 0
    assert report["realized_savings_usd"] == pytest.approx(rows["demo/payment-service"]["realized_savings_usd"], abs=0.01)
    cli.main(["realized", str(run)])
    assert "Realized savings since run 20260101-000000" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no analysis run to compare with"):
        cli.main(["realized", "--out-dir", str(tmp_path / "empty")])
