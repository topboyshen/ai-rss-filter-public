import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "cron_refresh.sh"


def _run(
    tmp_path,
    *,
    body='{"status":"queued"}',
    code="202",
    with_key=True,
    maintenance=False,
    lock_path=None,
    key="test-key",
    curl_exit=0,
):
    project = tmp_path / "project"
    data = project / "data"
    fake_bin = tmp_path / "bin"
    data.mkdir(parents=True)
    fake_bin.mkdir()
    if with_key:
        (project / ".env").write_text(
            f"AI_FILTER_API_KEY={shlex.quote(key)}\n", encoding="utf-8"
        )

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import stat
import sys
from pathlib import Path

args = sys.argv[1:]
header_arg = args[args.index('--header') + 1]
record = {'argv': args, 'key_in_env': 'AI_FILTER_API_KEY' in os.environ}
if header_arg.startswith('@'):
    header = Path(header_arg[1:])
    record.update(header=header.read_text(), header_path=str(header),
                  mode=stat.S_IMODE(header.stat().st_mode))
else:
    record['header'] = header_arg
Path(os.environ['FAKE_CURL_MARKER']).write_text(json.dumps(record))
Path(args[args.index('--output') + 1]).write_text(os.environ['FAKE_BODY'])
print(os.environ['FAKE_CODE'], end='')
sys.exit(int(os.environ['FAKE_CURL_EXIT']))
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    fake_flock.chmod(0o755)

    env = os.environ.copy()
    env.pop("AI_FILTER_API_KEY", None)
    env.pop("AI_FILTER_MAINTENANCE_FILE", None)
    env.update(
        AI_FILTER_ROOT=str(project),
        AI_FILTER_CRON_REFRESH_LOCK=str(
            lock_path if lock_path is not None else tmp_path / "refresh.lock"
        ),
        HEALTH_MAINTENANCE_FILE=str(tmp_path / "maintenance"),
        FAKE_BODY=body,
        FAKE_CODE=code,
        FAKE_CURL_MARKER=str(tmp_path / "curl-called"),
        FAKE_CURL_EXIT=str(curl_exit),
        TMPDIR=str(tmp_path),
        PATH=f"{fake_bin}:{env['PATH']}",
    )
    if maintenance:
        (tmp_path / "maintenance").write_text("planned maintenance\n", encoding="utf-8")
    return subprocess.run(
        ["bash", str(RUNNER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "code,body,curl_exit,expected",
    [
        ("202", '{"status":"queued"}', 0, 0),
        ("503", '{"error":"busy"}', 0, 22),
        ("000", "", 28, 28),
        ("200", "invalid json", 0, 1),
    ],
)
def test_secret_header_uses_private_file_and_is_cleaned_on_all_exits(
    tmp_path, code, body, curl_exit, expected
):
    key = "test-only-key;$(not-a-command)"
    result = _run(tmp_path, key=key, code=code, body=body, curl_exit=curl_exit)
    assert result.returncode == expected
    record = json.loads((tmp_path / "curl-called").read_text())
    assert all(key not in argument for argument in record["argv"])
    assert key not in result.stdout + result.stderr
    assert record["header"] == f"X-API-Key: {key}\n"
    assert record["mode"] == 0o600
    assert record["key_in_env"] is False
    assert not Path(record["header_path"]).exists()
    assert not list(tmp_path.glob("ai-filter-refresh.*"))


@pytest.mark.parametrize("key", ["safe\rInjected: value", "safe\nInjected: value"])
def test_key_with_cr_or_lf_is_rejected_before_curl(tmp_path, key):
    result = _run(tmp_path, key=key)
    assert result.returncode == 2
    assert "must not contain CR or LF" in result.stdout
    assert key not in result.stdout + result.stderr
    assert not (tmp_path / "curl-called").exists()
    assert not list(tmp_path.glob("ai-filter-refresh.*"))


def test_maintenance_sentinel_is_checked_before_env_and_curl(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    env_marker = tmp_path / "env-sourced"
    (project / ".env").write_text(
        f"printf 'sourced\\n' > {env_marker}\nAI_FILTER_API_KEY=test-key\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, maintenance=True, with_key=False)

    assert result.returncode == 0
    assert "maintenance: refresh trigger skipped" in result.stdout
    assert not env_marker.exists()
    assert not (tmp_path / "curl-called").exists()


def test_default_lock_moved_out_of_project_data():
    source = RUNNER.read_text(encoding="utf-8")

    assert "${AI_FILTER_CRON_REFRESH_LOCK:-/var/run/ai-filter-refresh.lock}" in source
    assert "data/.cron-refresh.lock" not in source


def test_symlink_lock_is_rejected_without_touching_target_or_curl(tmp_path):
    target = tmp_path / "target"
    target.write_text("unchanged\n", encoding="utf-8")
    lock = tmp_path / "refresh.lock"
    lock.symlink_to(target)

    result = _run(tmp_path, lock_path=lock)

    assert result.returncode == 78
    assert "regular non-symlink" in result.stderr
    assert "refusing unsafe refresh lock path" in result.stdout
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert not (tmp_path / "curl-called").exists()


def test_relative_lock_path_is_rejected_before_curl(tmp_path):
    result = _run(tmp_path, lock_path=Path("relative-refresh.lock"))

    assert result.returncode == 78
    assert "lock path must be absolute" in result.stderr
    assert not (tmp_path / "curl-called").exists()


def test_non_owner_only_existing_lock_is_rejected_before_open(tmp_path):
    lock = tmp_path / "refresh.lock"
    lock.write_text("unchanged\n", encoding="utf-8")
    lock.chmod(0o644)

    result = _run(tmp_path, lock_path=lock)

    assert result.returncode == 78
    assert "owner-only" in result.stderr
    assert lock.read_text(encoding="utf-8") == "unchanged\n"
    assert not (tmp_path / "curl-called").exists()


def test_missing_api_key_is_a_real_failure(tmp_path):
    result = _run(tmp_path, with_key=False)

    assert result.returncode == 2
    assert "AI_FILTER_API_KEY is unset" in result.stdout


def test_http_error_is_a_real_failure(tmp_path):
    result = _run(tmp_path, body='{"error":"busy"}', code="503")

    assert result.returncode == 22
    assert "HTTP 503" in result.stdout


def test_invalid_success_payload_is_rejected(tmp_path):
    result = _run(tmp_path, body='{"message":"not queued"}', code="200")

    assert result.returncode == 1
    assert "invalid success payload" in result.stdout


def test_queued_response_is_logged_on_its_own_line(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0
    assert "status=queued http=202" in result.stdout
    assert result.stdout.endswith("\n")
