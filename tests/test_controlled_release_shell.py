"""Existing shell release gates stay mandatory; controlled path never calls up."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

import test_release_script as release_helpers
from production_source_fixture_support import isolated_git_source

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_env(tmp_path, monkeypatch):
    files = ("scripts/release.sh", "scripts/capture_release_diagnostics.py", "scripts/run-release-gate.sh",
             "Dockerfile", "config.yaml", "docker-compose.yml")
    source = isolated_git_source(tmp_path / "source", ROOT, files=files)
    for name, path in {"PROJECT_ROOT": source, "RELEASE_SCRIPT": source / "scripts/release.sh",
                       "DIAGNOSTICS_HELPER": source / "scripts/capture_release_diagnostics.py",
                       "RELEASE_GATE_RUNNER": source / "scripts/run-release-gate.sh"}.items():
        monkeypatch.setattr(release_helpers, name, path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    fixture = release_helpers.release_env.__wrapped__(runtime)
    try:
        yield next(fixture)
    finally:
        fixture.close()


def test_pending_finalization_blocks_ordinary_release_without_docker(release_env):
    env, log, registry, _manifest = release_env
    marker = registry / "controlled-finalize.pending.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"original_operation":"must-query"}\n')
    marker.chmod(0o600)
    before = log.read_text() if log.exists() else ""
    result = release_helpers._run_release(env, "verify", "my-ai-filter:2026.10.27.999")
    assert result.returncode == 8 and "query its original operation" in result.stderr
    assert (log.read_text() if log.exists() else "") == before
    assert marker.read_text() == '{"original_operation":"must-query"}\n'


def test_controlled_check_requires_private_contract_and_inherited_lock(release_env):
    env, _log, _registry, _manifest = release_env
    result = release_helpers._run_release(env, "controlled-check", "my-ai-filter:synthetic", "a" * 64, "fresh",
                                         "my-ai-filter:old", "sha256:" + "b" * 64, "c" * 64, "preflight")
    assert result.returncode == 8


def test_release_fixture_uses_private_real_git_source_and_never_the_mount(release_env, tmp_path):
    env, _log, registry, _manifest = release_env
    source = release_helpers.PROJECT_ROOT
    assert source == tmp_path / "source" and source != ROOT
    assert source.stat().st_mode & 0o777 == 0o700 and registry.is_relative_to(tmp_path / "runtime")
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    assert len(head) == 40 and (Path(env["RELEASE_TEST_ATTESTATION_DIR"]) / (head + ".state")).is_file()
    for name in ("release.sh", "run-release-gate.sh", "capture_release_diagnostics.py"):
        assert (source / "scripts" / name).read_bytes() == (ROOT / "scripts" / name).read_bytes()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (source / "secrets").iterdir())
    env = {key: value for key, value in env.items() if not key.startswith("RELEASE_DIRTY_TREE_")
           and key != "RELEASE_ALLOW_DIRTY_TREE"}
    (source / "config.yaml").write_bytes((source / "config.yaml").read_bytes() + b"\n# synthetic source drift\n")
    result = release_helpers._run_release(env, "deploy", "my-ai-filter:2026.10.27.999")
    assert result.returncode == 7 and "dirty" in result.stderr.lower()
    assert not _log.exists()


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
