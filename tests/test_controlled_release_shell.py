"""Existing shell release gates stay mandatory; controlled path never calls up."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_release_script import release_env, _run_release  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_pending_finalization_blocks_ordinary_release_without_docker(release_env):  # noqa: F811
    env, log, registry, _manifest = release_env
    marker = registry / "controlled-finalize.pending.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"original_operation":"must-query"}\n')
    marker.chmod(0o600)
    before = log.read_text() if log.exists() else ""
    result = _run_release(env, "verify", "my-ai-filter:2026.10.27.999")
    assert result.returncode == 8 and "query its original operation" in result.stderr
    assert (log.read_text() if log.exists() else "") == before
    assert marker.read_text() == '{"original_operation":"must-query"}\n'


def test_controlled_check_requires_private_contract_and_inherited_lock(release_env):  # noqa: F811
    env, _log, _registry, _manifest = release_env
    result = _run_release(env, "controlled-check", "my-ai-filter:synthetic", "a" * 64, "fresh",
                          "my-ai-filter:old", "sha256:" + "b" * 64, "c" * 64, "preflight")
    assert result.returncode == 8


def _check_case(tmp_path):
    source = (ROOT / "scripts/release.sh").read_text()
    block = source.split('case "$cmd" in\n  controlled-check)\n', 1)[1].split('\n    ;;', 1)[0]
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
    import hashlib
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    # Run the real checked-in case body, replacing only external gate transports.
    names = ("require_clean_release_tree", "validate_image_ref", "verify_requested_digest", "assert_ref_consistency",
             "require_valid_release_test_attestation", "validate_build_contract_for_deploy",
             "validate_event_mapping_release_target", "release_compose", "validate_runtime_secrets_for_manifest",
             "validate_manifest_health_services", "wait_healthy")
    functions = "\n".join(f'{name}() {{ printf "%s\\n" {name} >> "$EVENTS"; [[ "$FAIL" != {name} ]]; }}' for name in names)
    script = tmp_path / "case.sh"
    script.write_text('set -euo pipefail\n' + functions + '''
resolved_image_id() {
  if [[ "$1" == "old:immutable" ]]; then printf '%s\n' "$OLD_DIGEST"; else printf 'sha256:%064d\n' 1; fi
}
manifest_path() { printf '%s\n' "$COMPOSE_FILE"; }
image_ref="$2"
''' + block + "\n")
    env = {**os.environ, "PRIVATE_CONTRACT": "synthetic-transport-only", "RELEASE_LOCK_FD": "10",
           "COMPOSE_FILE": str(manifest), "RELEASE_SOURCE_ID": "synthetic-clean",
           "HEALTH_SERVICES": "candidate-web candidate-worker", "EVENTS": str(tmp_path / "events"), "FAIL": ""}
    env["OLD_DIGEST"] = "sha256:" + "b" * 64
    args = ["bash", str(script), "controlled-check", "image:immutable", digest, "fresh", "old:immutable",
            "sha256:" + "b" * 64, "c" * 64, "health"]
    return args, env, names


@pytest.mark.parametrize("fail", ["require_clean_release_tree", "require_valid_release_test_attestation",
    "validate_build_contract_for_deploy", "validate_event_mapping_release_target", "validate_manifest_health_services", "wait_healthy"])
def test_actual_controlled_case_never_bypasses_a_failed_gate(tmp_path, fail):
    args, env, _names = _check_case(tmp_path)
    env["FAIL"] = fail
    result = subprocess.run(args, env=env, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert Path(env["EVENTS"]).read_text().splitlines()[-1] == fail
    assert b"controlled-check:ok" not in result.stdout


def test_preflight_skips_only_health_recovery_skips_only_fresh_attestation(tmp_path):
    args, env, _names = _check_case(tmp_path)
    args[-1] = "preflight"
    result = subprocess.run(args, env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    events = Path(env["EVENTS"])
    assert "require_valid_release_test_attestation" in events.read_text() and "wait_healthy" not in events.read_text()
    events.unlink()
    args[5], args[-1] = "recovery", "health"
    result = subprocess.run(args, env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert "wait_healthy" in events.read_text() and "require_valid_release_test_attestation" not in events.read_text()
    assert "validate_build_contract_for_deploy" in events.read_text() and "validate_event_mapping_release_target" in events.read_text()


@pytest.mark.parametrize("failure", [False, True])
def test_real_check_child_preserves_output_without_running_containers(tmp_path, failure):
    from scripts import controlled_release_finalize as final
    tmp_path.chmod(0o700)
    lock = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    command = [sys.executable, "-I", "-c", "import sys;print('synthetic original check');sys.exit(" + str(int(failure)) + ")"]
    try:
        if failure:
            with pytest.raises(final.gate.AdmissionError, match="controlled-check failed"):
                final._check_process(command, os.environ.copy(), lock, evidence_root=tmp_path)
        else:
            assert final._check_process(command, os.environ.copy(), lock, evidence_root=tmp_path)["exit_code"] == 0
        assert (tmp_path / "check-00.log").read_bytes() == b"synthetic original check\n"
        assert final.gate._read(tmp_path / "check-00.json")["exit_code"] == int(failure)
    finally:
        os.close(lock)
