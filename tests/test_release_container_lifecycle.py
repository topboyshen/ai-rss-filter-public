import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.check_release_container_lifecycle import check

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", [
    {"AI_FILTER_PRODUCTION_STARTUP_OPERATION": "a" * 64},
    ["AI_FILTER_PRODUCTION_STARTUP_ROOT=/controlled"],
    {"indirect": "${AI_FILTER_PRODUCTION_STARTUP_OPERATION:?}"},
])
def test_granted_manifest_cannot_enter_ordinary_recreate(tmp_path, value):
    path = tmp_path / "compose.json"
    path.write_text(json.dumps({"services": {"api": {"environment": value}}}))
    with pytest.raises(ValueError, match="recreate is forbidden"):
        check(path)


def test_normal_manifest_and_ordinary_release_path_remain_usable(tmp_path):
    path = tmp_path / "compose.yml"
    path.write_text("services:\n  api:\n    image: synthetic\n    environment:\n      AI_FILTER_ROLE: web\n")
    assert check(path) is None


@pytest.mark.parametrize("unsafe", ["symlink", "empty", "oversized", "invalid"])
def test_bad_manifest_fails_closed(tmp_path, unsafe):
    path = tmp_path / "compose.json"
    path.write_bytes(b"{}")
    if unsafe == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    elif unsafe == "empty":
        path.write_bytes(b"")
    elif unsafe == "oversized":
        path.write_bytes(b" " * (4 * 1024**2 + 1))
    with pytest.raises((OSError, ValueError)):
        check(path)


@pytest.mark.parametrize("controlled", [True, False])
def test_actual_compose_up_guard_precedes_every_mutation(tmp_path, controlled):
    manifest = tmp_path / "manifest.json"
    service = {"image": "synthetic"}
    if controlled:
        service["environment"] = {"AI_FILTER_PRODUCTION_STARTUP_ROOT": "/grants"}
    manifest.write_text(json.dumps({"services": {"api": service}}))
    source = (ROOT / "scripts/release.sh").read_text()
    function = source[source.index("compose_up() {"):source.index("\nservice_state() {")]
    # Invoke the real shell function. Only external Docker dispatch is replaced.
    program = 'ROOT="$1"; MANIFEST="$2"; LOG="$3"\n'
    program += 'validate_runtime_digest() { :; }; manifest_path() { printf "%s" "$MANIFEST"; }\n'
    program += 'release_compose() { printf "%s\\n" "$*" >> "$LOG"; }\n'
    program += function + '\ncompose_up sha256:synthetic archived\n'
    log = tmp_path / "commands"
    # release uses python3 -I, so inherit this test runtime's dependency path.
    env = dict(__import__("os").environ)
    env["PATH"] = str(Path(sys.executable).parent) + ":" + env["PATH"]
    result = subprocess.run(["bash", "-c", program, "fixture", str(ROOT), str(manifest), str(log)],
                            env=env, capture_output=True, text=True, timeout=10)
    if controlled:
        assert result.returncode == 3 and "recreate is forbidden" in result.stderr
        assert not log.exists()
    else:
        assert result.returncode == 0
        assert "up -d --no-build --pull never --remove-orphans" in log.read_text()
