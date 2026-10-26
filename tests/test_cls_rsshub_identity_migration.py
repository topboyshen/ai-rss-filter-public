"""CLS RSS identity contract and real signed-webhook/processor regressions.

Unit fixtures deliberately use opaque invented Miniflux hashes, NOT a local
implementation of its hashing algorithm. Set AI_FILTER_MINIFLUX_REPORT to the
isolated ``check_miniflux_rsshub_identity.py --run`` report to repeat these
end-to-end assertions with entries produced/stored by actual Miniflux 2.3.2.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from db import database
from scripts import check_miniflux_rsshub_identity as migration
from test_webhook_shared_section_links import _score
from test_webhook_shared_section_links import cfg as cfg
from test_webhook_shared_section_links import deliver as deliver


def _contract_report():
    """Invented API-shaped identities for unit tests; no Miniflux claim."""
    snapshots = {}
    for case in migration.CASES:
        old = []
        for index in (1, 2):
            title = f"合成{'甲' if case == 'duplicate' or index == 1 else '乙'}公司披露公告"
            url = migration.SECTION
            if case == "watch":
                url = f"https://api3.cls.cn/share/article/900000{index}?os=web&sv=8.7.9&app="
            old.append({"id": index, "hash": str(index) * 64, "url": url,
                        "title": title, "content": f"合成测试正文{index}"})
        compat = [dict(entry, content=entry["content"] + "。兼容刷新。") for entry in old]
        candidate = deepcopy(compat)
        if case != "watch":
            candidate.extend(dict(entry, id=entry["id"] + 2, hash=str(entry["id"] + 2) * 64,
                                  url=f"https://www.cls.cn/detail/900000{entry['id']}") for entry in old)
        snapshots[case] = {"old": old, "compat": compat, "candidate": candidate}
    hashes = {f"{case}-{mode}.xml": hashlib.sha256(migration.synthetic_rss(case, mode)).hexdigest()
              for case in migration.CASES for mode in migration.MODES}
    requests = [{"case": case, "mode": mode, "sha256": hashes[f"{case}-{mode}.xml"]}
                for case in migration.CASES for mode in migration.MODES]
    return {"schema": 1, "version": "2.3.2", "actual_version": {"version": "2.3.2"},
            "cleanup": {"containers": [], "network_removed": True}, "snapshots": snapshots,
            "fixture_sha256": hashes, "fixture_requests": requests}


def _counts():
    with database.get_connection() as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("news_cache", "events", "event_evidence", "backtest_signals")}


def _exercise(stages, case, deliver, monkeypatch):
    calls = []
    old_task = deliver(stages["old"])
    _score(old_task, monkeypatch, calls)
    original = _counts()
    assert len(calls) == 2
    assert original["news_cache"] == original["events"] == 2
    links = [row["url"] for row in old_task["entries"]]
    if case != "watch":
        expected = [migration.SECTION + "?ai_filter_item=h-" + entry["hash"] for entry in stages["old"]]
        assert links == expected
    compat_task = deliver(stages["compat"])
    _score(compat_task, monkeypatch, calls)
    assert [row["url"] for row in compat_task["entries"]] == links
    assert len(calls) == 2
    assert _counts() == original  # no new score/cache/event/evidence/ledger row
    _score(deliver(stages["candidate"]), monkeypatch, calls)
    after = _counts()
    if case == "watch":
        assert len(calls) == 2
        assert after == original
    else:
        assert len(calls) == 4  # raw candidate really exposes the identity break
        assert after["news_cache"] == after["events"] == 4


@pytest.mark.parametrize("case", migration.CASES)
def test_contract_entries_preserve_legacy_identity_and_raw_candidate_detects_break(case, deliver, monkeypatch):
    report = _contract_report()
    migration.check_report(report)
    _exercise(report["snapshots"][case], case, deliver, monkeypatch)


@pytest.mark.parametrize("case", migration.CASES)
def test_actual_miniflux_migration_report_through_webhook(case, deliver, monkeypatch):
    path = os.environ.get("AI_FILTER_MINIFLUX_REPORT") or os.environ.get("MINIFLUX_IDENTITY_EVIDENCE")
    if not path:
        pytest.skip("explicit native Miniflux/PostgreSQL report required; ordinary CI is offline")
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    migration.check_report(report)
    _exercise(report["snapshots"][case], case, deliver, monkeypatch)


@pytest.mark.parametrize("case", migration.CASES)
def test_synthetic_rss_retains_old_identity_fields_without_reusing_real_news(case):
    parsed = {mode: ET.fromstring(migration.synthetic_rss(case, mode)) for mode in migration.MODES}
    fields = {}
    for mode, root in parsed.items():
        assert root.findtext("channel/link") == migration.SECTION
        items = root.findall("channel/item")
        assert len(items) == 2
        assert all("合成" in item.findtext("title") for item in items)
        assert all(item.find("guid").get("isPermaLink") == "false" for item in items)
        fields[mode] = [(item.findtext("title"), item.findtext("guid"), item.findtext("link")) for item in items]
    assert fields["compat"] == fields["old"]
    assert (fields["candidate"] == fields["old"]) is (case == "watch")


@pytest.mark.parametrize("field,value", [("id", 999), ("hash", "f" * 64),
                                         ("url", "https://www.cls.cn/detail/999")])
def test_report_rejects_change_to_any_legacy_identity_field(field, value):
    report = _contract_report()
    report["snapshots"]["default"]["compat"][0][field] = value
    with pytest.raises(ValueError, match="changed identity"):
        migration.check_report(report)


@pytest.mark.parametrize("mode", migration.MODES)
def test_report_requires_every_stage(mode):
    report = _contract_report()
    del report["snapshots"]["default"][mode]
    with pytest.raises(ValueError, match="missing migration stage"):
        migration.check_report(report)


@pytest.mark.parametrize("mutation", ["candidate_unchanged", "missing_duplicate", "watch_rewritten",
                                      "cleanup", "error", "version", "missing_fetch"])
def test_report_rejects_false_positive_acceptance(mutation):
    report = _contract_report()
    if mutation == "candidate_unchanged":
        report["snapshots"]["default"]["candidate"] = report["snapshots"]["default"]["old"]
    elif mutation == "missing_duplicate":
        report["snapshots"]["duplicate"]["old"].pop()
    elif mutation == "watch_rewritten":
        report["snapshots"]["watch"]["candidate"][0]["url"] = migration.SECTION
    elif mutation == "cleanup":
        report["cleanup"]["network_removed"] = False
    elif mutation == "error":
        report["error"] = "fixture failure"
    elif mutation == "missing_fetch":
        report["fixture_requests"].pop()
    else:
        report["actual_version"]["version"] = "2.3.1"
    with pytest.raises(ValueError):
        migration.check_report(report)


def test_digest_only_and_no_implicit_docker_or_model_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(migration.subprocess, "run", lambda *_a, **_k: pytest.fail("offline check used Docker"))
    assert migration.check_report(_contract_report())["model_calls"] == 0
    assert not migration.PIN.fullmatch("miniflux/miniflux:2.3.2")
    assert migration.PIN.fullmatch("miniflux/miniflux@sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="new directory"):
        migration.run(tmp_path, {})


def test_runner_refuses_non_disposable_host_before_docker(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(migration.subprocess, "run", lambda *_a, **_k: pytest.fail("must not query Docker"))
    with pytest.raises(ValueError, match="disposable"):
        migration.Sandbox(tmp_path, {}).inspect_images()


def test_cleanup_total_deadline_records_unremoved_resources_without_docker(monkeypatch, tmp_path):
    sandbox = migration.Sandbox(tmp_path, {})
    sandbox.containers = ["owned-test-container"]
    sandbox.network_created = True
    clock = iter((100, 200, 200))  # cleanup budget=145; both subsequent calls are expired
    monkeypatch.setattr(migration.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(migration.subprocess, "run", lambda *_a, **_k: pytest.fail("expired cleanup ran Docker"))
    result = sandbox.cleanup()
    assert result == {"containers": [{"name": "owned-test-container", "error": "TimeoutError"}],
                      "network_removed": False}


def test_cleanup_timeout_does_not_prevent_final_failure_report(monkeypatch, tmp_path):
    def fail_start(sandbox):
        sandbox.containers = ["owned-test-container"]
        sandbox.network_created = True
        raise ValueError("synthetic workload failure")

    def expired(*_args, **_kwargs):
        raise TimeoutError("synthetic cleanup deadline exhausted")

    monkeypatch.setattr(migration.Sandbox, "inspect_images", lambda _self: {})
    monkeypatch.setattr(migration.Sandbox, "start", fail_start)
    monkeypatch.setattr(migration.Sandbox, "command", expired)
    monkeypatch.setattr(migration.subprocess, "run", lambda *_a, **_k: pytest.fail("test touched Docker"))
    output = tmp_path / "failed-run"
    with pytest.raises(ValueError):
        migration.run(output, {})
    report = json.loads((output / "report.json").read_text())
    assert report["error"] == "ValueError: synthetic workload failure"
    assert report["cleanup"] == {
        "containers": [{"name": "owned-test-container", "error": "TimeoutError"}],
        "network_removed": False,
    }
