import time

import pytest

from services.market_close_result import (
    persist_host_fallback,
    read_result,
    result_dir_from_env,
    write_result,
)


def test_write_result_is_atomic_and_private(tmp_path):
    path = write_result(
        tmp_path,
        kind="collector",
        status="success",
        trade_date="2026-09-11",
        due_total=10,
        unresolved=0,
        written=10,
        exit_code=0,
    )
    payload = read_result(path)
    assert payload["version"] == 1
    assert payload["kind"] == "collector"
    assert payload["status"] == "success"
    assert payload["trade_date"] == "2026-09-11"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".tmp").exists()


def test_result_dir_from_env_requires_absolute_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", "relative/path")
    with pytest.raises(ValueError, match="absolute"):
        result_dir_from_env(db_path=tmp_path / "cache.db")

    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", str(tmp_path / "results"))
    assert result_dir_from_env() == tmp_path / "results"


def test_write_result_rejects_unknown_status(tmp_path):
    with pytest.raises(ValueError, match="status"):
        write_result(tmp_path, kind="collector", status="healthy")


def test_host_fallback_skips_when_same_run_image_already_wrote(tmp_path):
    started = int(time.time())
    write_result(tmp_path, kind="collector", status="success", run_id="a" * 32,
                 trade_date="2026-09-11")
    assert persist_host_fallback(
        tmp_path, exit_code=0, output="", started_at=started, run_id="a" * 32
    ) is None
    assert read_result(tmp_path / "collector_last.json")["status"] == "success"


def test_host_fallback_keeps_previous_result_on_nontrading_skip(tmp_path):
    write_result(tmp_path, kind="collector", status="success", trade_date="2026-09-10")
    path = tmp_path / "collector_last.json"
    previous = read_result(path)
    earlier = 1_000_000
    path.chmod(0o600)
    assert persist_host_fallback(
        tmp_path,
        exit_code=0,
        output="[MarketClose] 2026-09-11 非交易日，跳过采集",
        started_at=earlier,
        run_id="b" * 32,
        previous_result=previous,
    ) is None
    assert read_result(path)["trade_date"] == "2026-09-10"


def test_host_fallback_records_incomplete_from_collector_error(tmp_path):
    path = persist_host_fallback(
        tmp_path,
        exit_code=1,
        output="[MarketClose] 指数完整但待结算覆盖未完成；有效行情已保存；missing=1",
        started_at=1_000_000,
        run_id="b" * 32,
    )
    payload = read_result(path)
    assert payload["status"] == "incomplete"
    assert payload["detail"] == "host-fallback"
    assert payload["exit_code"] == 1


@pytest.mark.parametrize("old_run", ["a" * 32, "b" * 32])
def test_host_failure_replaces_even_recent_success(tmp_path, old_run):
    # Even the same run's success cannot conceal a subsequent process failure.
    write_result(tmp_path, kind="collector", status="success", run_id=old_run)
    path = persist_host_fallback(
        tmp_path, exit_code=1, output="failed", started_at=int(time.time()),
        run_id="b" * 32,
    )
    payload = read_result(path)
    assert payload["run_id"] == "b" * 32
    assert payload["status"] == "error"


def test_host_fallback_keeps_current_incomplete_receipt(tmp_path):
    path = write_result(tmp_path, kind="collector", status="incomplete",
                        run_id="a" * 32, unresolved=3)
    original = path.read_bytes()
    assert persist_host_fallback(
        tmp_path, exit_code=1, output="failure", started_at=1, run_id="a" * 32,
    ) is None
    assert path.read_bytes() == original


def test_collector_receipt_inherits_runner_identity_only_for_collector(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_CLOSE_RUN_ID", "a" * 32)
    collector = write_result(tmp_path, kind="collector", status="success")
    repair = write_result(tmp_path, kind="repair", status="complete")
    assert read_result(collector)["run_id"] == "a" * 32
    assert read_result(repair)["run_id"] != "a" * 32


def test_nontrading_log_does_not_hide_failed_run(tmp_path):
    path = persist_host_fallback(
        tmp_path, exit_code=1, output="非交易日，跳过采集", started_at=1,
        run_id="a" * 32,
    )
    assert read_result(path)["status"] == "error"


def test_host_fallback_replaces_its_own_unfinished_marker(tmp_path):
    write_result(tmp_path, kind="collector", status="error", run_id="a" * 32,
                 detail="collector attempt has not completed")
    path = persist_host_fallback(
        tmp_path, exit_code=0, output="", started_at=1, run_id="a" * 32,
    )
    assert read_result(path)["status"] == "success"
