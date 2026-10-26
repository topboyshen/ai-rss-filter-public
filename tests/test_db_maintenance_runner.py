"""Weekly maintenance must report actual phase results, not just a printed ok."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts import run_db_maintenance as maintenance


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_db_maintenance.py"
RUN_ID = "maintenance-regression"


def _records(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def _summary(records, scope="database"):
    return next(
        r for r in reversed(records) if r["scope"] == scope and r["event"] == "finish"
    )


def _database(tmp_path):
    path = tmp_path / "cache.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER CHECK (id > 0))")
        connection.execute("INSERT INTO items VALUES (1)")
    return path


@pytest.mark.parametrize(
    "db_args",
    [
        ["--db", "/tmp/maintenance-test.db"],
        ["--db=/tmp/maintenance-test.db"],
        ["--db", "/app/data/cache.db"],
        ["--db", ""],
        ["--db="],
    ],
)
@pytest.mark.parametrize("host_first", [True, False])
def test_cli_rejects_host_with_explicit_db_before_any_side_effect(
    db_args, host_first, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid arguments must not start maintenance or open a database")

    monkeypatch.setattr(maintenance, "host_maintenance", forbidden)
    monkeypatch.setattr(maintenance, "database_maintenance", forbidden)
    monkeypatch.setattr(maintenance.subprocess, "Popen", forbidden)
    monkeypatch.setattr(maintenance.sqlite3, "connect", forbidden)
    args = ["--host", *db_args] if host_first else [*db_args, "--host"]

    with pytest.raises(SystemExit) as error:
        maintenance.main([*args, "--run-id", RUN_ID])

    assert error.value.code == 2
    output = capsys.readouterr()
    assert "not allowed with argument" in output.err
    assert "--host" in output.err and "--db" in output.err
    assert output.out == ""


@pytest.mark.parametrize("host_first", [True, False])
def test_real_cli_rejects_conflicting_modes_before_external_commands(
    tmp_path, host_first
):
    path = tmp_path / "must-not-exist.db"
    args = ["--host", f"--db={path}"]
    if not host_first:
        args.reverse()
    env = {**os.environ, "PATH": str(tmp_path)}

    result = subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr
    assert result.stdout == ""
    assert not path.exists()


@pytest.mark.parametrize("equals_form", [True, False])
def test_real_cli_explicit_db_maintains_only_the_selected_database(
    tmp_path, equals_form
):
    path = _database(tmp_path)
    db_args = [f"--db={path}"] if equals_form else ["--db", str(path)]

    result = subprocess.run(
        [sys.executable, str(RUNNER), *db_args, "--run-id", RUN_ID],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _summary(_records(result.stdout))["rc"] == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM items").fetchall() == [(1,)]


def test_cli_without_mode_keeps_default_container_database(monkeypatch):
    calls = []

    def database(path, run_id):
        calls.append((path, run_id))
        return 0

    monkeypatch.setattr(maintenance, "database_maintenance", database)

    assert maintenance.main(["--run-id", RUN_ID]) == 0
    assert calls == [("/app/data/cache.db", RUN_ID)]


def test_real_sqlite_success_logs_ordered_utc_phases_and_keeps_rows(tmp_path, capsys):
    path = _database(tmp_path)

    assert maintenance.database_maintenance(str(path), RUN_ID) == 0

    records = _records(capsys.readouterr().out)
    assert records[0]["event"] == "start"
    assert [
        (r["phase"], r["status"]) for r in records if r["event"] == "phase_end"
    ] == [
        ("connect", "success"),
        ("integrity_check", "success"),
        ("vacuum", "success"),
        ("close", "success"),
    ]
    assert _summary(records)["rc"] == 0
    assert all(r["run_id"] == RUN_ID for r in records)
    timestamps = [datetime.fromisoformat(r["utc"]) for r in records]
    assert all(stamp.utcoffset() == timedelta(0) for stamp in timestamps)
    assert timestamps == sorted(timestamps)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM items").fetchall() == [(1,)]


def test_real_non_ok_integrity_result_skips_vacuum(tmp_path, monkeypatch, capsys):
    path = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("INSERT INTO items VALUES (-1)")
    connect = sqlite3.connect
    statements = []

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(maintenance.sqlite3, "connect", traced_connect)

    assert maintenance.database_maintenance(str(path), RUN_ID) == 1

    records = _records(capsys.readouterr().out)
    result = _summary(records)
    assert (result["integrity_check"], result["vacuum"], result["close"]) == (
        "failed",
        "skipped",
        "success",
    )
    assert "VACUUM" not in statements
    failure = next(r for r in records if r.get("reason") == "non_ok_result")
    assert "CHECK constraint failed" in failure["results"][0][0]


def test_real_writer_lock_reports_vacuum_failure_without_retry_or_data_change(
    tmp_path, capsys
):
    path = _database(tmp_path)
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO items VALUES (2)")
        assert maintenance.database_maintenance(str(path), RUN_ID) == 1
        writer.rollback()

    records = _records(capsys.readouterr().out)
    result = _summary(records)
    assert (result["integrity_check"], result["vacuum"], result["close"]) == (
        "success",
        "failed",
        "success",
    )
    failures = [r for r in records if r.get("error_type") == "OperationalError"]
    assert len(failures) == 1
    assert failures[0]["phase"] == "vacuum"
    assert "locked" in failures[0]["error"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM items").fetchall() == [(1,)]


@pytest.mark.parametrize("invalid", ["missing/parent.db", "directory"])
def test_real_connection_failure_reports_skipped_phases(tmp_path, capsys, invalid):
    path = tmp_path / invalid
    if invalid == "directory":
        path.mkdir()

    assert maintenance.database_maintenance(str(path), RUN_ID) == 1

    result = _summary(_records(capsys.readouterr().out))
    assert result["integrity_check"] == result["vacuum"] == result["close"] == "skipped"
    assert not (tmp_path / "missing").exists()


def test_real_corrupt_file_reports_integrity_exception_and_close(tmp_path, capsys):
    path = tmp_path / "corrupt.db"
    original = b"this is not a SQLite database\n" * 100
    path.write_bytes(original)

    assert maintenance.database_maintenance(str(path), RUN_ID) == 1

    records = _records(capsys.readouterr().out)
    result = _summary(records)
    assert (result["integrity_check"], result["vacuum"], result["close"]) == (
        "failed",
        "skipped",
        "success",
    )
    assert any(r.get("error_type") == "DatabaseError" for r in records)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "error,rc",
    [
        (RuntimeError("check failed"), 1),
        (SystemExit(7), 7),
        (SystemExit(0), 1),
        (KeyboardInterrupt(), 130),
    ],
)
def test_phase_exception_or_exit_still_closes_and_reports_failure(
    monkeypatch, capsys, error, rc
):
    class FailingConnection:
        closed = False

        def execute(self, statement):
            raise error

        def close(self):
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(maintenance.sqlite3, "connect", lambda _: connection)

    assert maintenance.database_maintenance("unused", RUN_ID) == rc

    result = _summary(_records(capsys.readouterr().out))
    assert connection.closed
    assert result["rc"] == rc
    assert result["integrity_check"] == "failed"
    assert result["vacuum"] == "skipped"
    assert result["close"] == "success"


def test_close_exception_cannot_report_success(tmp_path, monkeypatch, capsys):
    real_connection = sqlite3.connect(_database(tmp_path))

    class CloseFailure:
        def execute(self, statement):
            return real_connection.execute(statement)

        def close(self):
            real_connection.close()
            raise sqlite3.OperationalError("close failed")

    monkeypatch.setattr(maintenance.sqlite3, "connect", lambda _: CloseFailure())

    assert maintenance.database_maintenance("unused", RUN_ID) == 1

    result = _summary(_records(capsys.readouterr().out))
    assert result["integrity_check"] == result["vacuum"] == "success"
    assert result["close"] == "failed"
    assert result["rc"] == 1


def _host_env(tmp_path, *, mode="database", timeout_seconds=10):
    """Local subprocesses exercise stdin delivery and actual timeout, no Docker."""
    binary = tmp_path / "bin"
    binary.mkdir()
    timeout = binary / "timeout"
    timeout.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys\n"
        "assert sys.argv[1:4] == ['--signal=TERM', '--kill-after=60s', '60m']\n"
        "process = subprocess.Popen(sys.argv[4:], start_new_session=True)\n"
        "try:\n"
        "    rc = process.wait(timeout=float(os.environ['TEST_TIMEOUT_SECONDS']))\n"
        "except subprocess.TimeoutExpired:\n"
        "    os.killpg(process.pid, signal.SIGTERM)\n"
        "    process.wait(timeout=5)\n"
        "    raise SystemExit(124)\n"
        "raise SystemExit(128 - rc if rc < 0 else rc)\n",
        encoding="utf-8",
    )
    docker = binary / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys\n"
        "assert sys.argv[1:7] == ['exec', '-i', 'ai-rss-filter-ai-filter-api-1', 'python', '-u', '-']\n"
        "mode = os.environ['TEST_DOCKER_MODE']\n"
        "if mode == 'launch_failure':\n"
        "    print('container unavailable', file=sys.stderr)\n"
        "    raise SystemExit(125)\n"
        "if mode == 'empty_success':\n"
        "    raise SystemExit(0)\n"
        "if mode == 'killed':\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "source = sys.stdin.buffer.read()\n"
        "raise SystemExit(subprocess.run([sys.executable, '-u', '-', *sys.argv[7:], '--db', os.environ['TEST_DB']], input=source).returncode)\n",
        encoding="utf-8",
    )
    timeout.chmod(0o755)
    docker.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=str(binary),
        TEST_TIMEOUT_SECONDS=str(timeout_seconds),
        TEST_DOCKER_MODE=mode,
        TEST_DB=str(_database(tmp_path)),
    )
    return env


def _run_host(env):
    return subprocess.run(
        [sys.executable, str(RUNNER), "--host", "--run-id", RUN_ID],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_host_streams_exact_source_via_stdin_and_reports_verified_success(tmp_path):
    result = _run_host(_host_env(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    records = _records(result.stdout)
    assert records[0]["scope"] == records[-1]["scope"] == "host"
    summary = _summary(records, "host")
    assert summary["outcome"] == "success"
    assert (
        summary["rc"] == summary["transport_rc"] == summary["database_result_rc"] == 0
    )
    assert all(summary[phase] == "success" for phase in maintenance.PHASES)


def test_host_propagates_real_database_failure_and_skipped_vacuum(tmp_path):
    env = _host_env(tmp_path)
    with sqlite3.connect(env["TEST_DB"]) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("INSERT INTO items VALUES (-1)")

    result = _run_host(env)

    assert result.returncode == 1
    summary = _summary(_records(result.stdout), "host")
    assert (
        summary["rc"] == summary["transport_rc"] == summary["database_result_rc"] == 1
    )
    assert summary["outcome"] == "failed"
    assert summary["integrity_check"] == "failed"
    assert summary["vacuum"] == "skipped"
    assert summary["close"] == "success"


@pytest.mark.parametrize(
    "mode,rc",
    [
        ("launch_failure", 125),
        ("empty_success", 1),
        ("killed", 137),
    ],
)
def test_transport_failure_or_missing_results_cannot_report_success(tmp_path, mode, rc):
    result = _run_host(_host_env(tmp_path, mode=mode))

    assert result.returncode == rc
    summary = _summary(_records(result.stdout), "host")
    assert summary["rc"] == rc
    assert summary["container_exit"] == "unknown"
    assert all(summary[phase] == "unknown" for phase in maintenance.PHASES)
    assert summary["outcome"] != "success"
    if mode == "empty_success":
        assert summary["transport_rc"] == 0
    if mode == "killed":
        assert summary["outcome"] == "killed"


def test_actual_subprocess_timeout_retains_observed_phase_and_marks_unfinished_unknown(
    tmp_path,
):
    env = _host_env(tmp_path, timeout_seconds=1)
    with sqlite3.connect(env["TEST_DB"]) as writer:
        writer.execute("BEGIN IMMEDIATE")
        result = _run_host(env)
        writer.rollback()

    assert result.returncode == 124
    records = _records(result.stdout)
    assert not any(r["scope"] == "database" and r["event"] == "finish" for r in records)
    summary = _summary(records, "host")
    assert summary["outcome"] == "timeout"
    assert summary["rc"] == summary["transport_rc"] == 124
    assert summary["integrity_check"] == "success"
    assert (
        summary["vacuum"] == summary["close"] == summary["container_exit"] == "unknown"
    )
    assert summary["database_result_rc"] is None


def test_host_missing_timeout_command_reports_launch_failure(tmp_path):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()

    result = _run_host({**os.environ, "PATH": str(empty_bin)})

    assert result.returncode == 69
    summary = _summary(_records(result.stdout), "host")
    assert summary["outcome"] == "launch_failed"
    assert all(summary[phase] == "skipped" for phase in maintenance.PHASES)
    assert summary["transport_rc"] is None


def test_host_read_error_preserves_already_observed_phase(monkeypatch, capsys):
    class ReadFailure:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def stdout(self):
            yield (
                json.dumps(
                    {
                        "scope": "database",
                        "run_id": RUN_ID,
                        "event": "phase_end",
                        "phase": "integrity_check",
                        "status": "success",
                    }
                )
                + "\n"
            )
            raise OSError("lost output stream")

    monkeypatch.setattr(
        maintenance.subprocess, "Popen", lambda *args, **kwargs: ReadFailure()
    )

    assert maintenance.host_maintenance(RUN_ID) == 69

    summary = _summary(_records(capsys.readouterr().out), "host")
    assert summary["outcome"] == "transport_error"
    assert summary["integrity_check"] == "success"
    assert summary["vacuum"] == summary["close"] == "unknown"
    assert summary["container_exit"] == "unknown"


def test_host_transport_close_error_after_zero_exit_cannot_report_success(
    monkeypatch, capsys
):
    class CloseFailure:
        stdout = ()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise OSError("transport close failed")

        def wait(self):
            return 0

    monkeypatch.setattr(
        maintenance.subprocess, "Popen", lambda *args, **kwargs: CloseFailure()
    )

    assert maintenance.host_maintenance(RUN_ID) == 69

    summary = _summary(_records(capsys.readouterr().out), "host")
    assert summary["rc"] == 69
    assert summary["outcome"] == "transport_error"
    assert summary["container_exit"] == "unknown"


def test_cron_preserves_schedule_log_path_and_no_backup_policy():
    cron = (ROOT / "ops/cron/ai-filter.crontab").read_text(encoding="utf-8")
    lines = [line for line in cron.splitlines() if line.startswith("0 3 * * 0 ")]
    assert len(lines) == 1
    assert (
        "umask 077; python3 /root/ai-rss-filter/scripts/run_db_maintenance.py --host"
        in lines[0]
    )
    assert lines[0].endswith(">> /var/log/ai-filter-db-maintenance.log 2>&1")
    assert "# NO_BACKUP 2026-09-10 | 30 3 * * *" in cron
    assert "# NO_BACKUP 2026-09-10 | 30 4 * * 0" in cron
