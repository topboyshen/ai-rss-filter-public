"""Every adapter change selects the real capped native suite; no broad exemption."""
from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PATH = ".github/workflows/cutover-completion.yml"


def selected(path, patterns):
    for pattern in patterns:
        expression = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if re.fullmatch(expression, path):
            return True
    return False


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("path", [
    "services/production_release_lock.py", "services/production_admission_lifecycle.py",
    "services/admission_fence.py", "services/production_activation_control.py",
    "scripts/private_control_subpaths.py", "scripts/controlled_production_release.py",
    "scripts/controlled_release_finalize.py", "scripts/private_release_contract.py",
    "scripts/sender_namespace_http.py", "scripts/production_gateway_switch.py",
    "scripts/production_legacy_takeover.py", "scripts/production_admission_watchdog.py",
    "scripts/production_admission_supervisor.py", "tests/test_sender_namespace_http.py",
    "scripts/production_gateway_native.py", "scripts/production_namespace_native.py",
    "scripts/production_host_ingress_native.py", "tests/test_production_host_ingress_native.py",
    "scripts/production_sender_bootstrap.py", "tests/test_production_namespace_native.py",
    "scripts/production_sender_bootstrap_native.py",
    "scripts/production_receiver_bootstrap.py", "scripts/production_receiver_bootstrap_native.py",
    "tests/test_production_receiver_bootstrap.py",
    "tests/test_production_receiver_bootstrap_native.py",
    "scripts/production_sender_native_bridge.py", "tests/test_sender_external_postgres_identity.py",
    "tests/production_admission_environment_support.py",
    "scripts/verify_production_activation_native.py", "scripts/production_activation_fixture.py",
    "scripts/release.sh", "tests/test_production_release_lock.py",
    "tests/test_private_release_control_subpaths.py", "tests/test_controlled_production_release.py",
    "tests/test_controlled_release_finalize.py", "tests/test_controlled_finalize_cli.py", "tests/test_controlled_release_shell.py",
    "tests/test_production_release_adapter_workflow_paths.py", PATH,
])
def test_each_real_adapter_path_triggers_native(event, path):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    assert selected(path, workflow["on"][event]["paths"])


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("path", [
    "docs/production-release.md", "README.md", "core/scorer.py",
    "scripts/controlled_production_release.py.bak", "scripts/nested/private_control_subpaths.py",
    "scripts/production_receiver_bootstrap.py.bak", "scripts/nested/production_receiver_bootstrap_native.py",
    "scripts/production_host_ingress_native.py.bak", "scripts/nested/production_host_ingress_native.py",
    "tests/nested/test_controlled_release_finalize.py", "tests/test_unrelated.py",
])
def test_unrelated_and_lookalike_paths_do_not_trigger(event, path):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    assert not selected(path, workflow["on"][event]["paths"])


def test_original_resources_and_complete_native_steps_remain():
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["native-amd64"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "15"
    pg = job["services"]["postgres"]["options"]
    for token in ("--memory 384m", "--memory-swap 384m", "--pids-limit 64"):
        assert token in pg
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    for script in ("verify_cutover_completion_native.py", "verify_production_startup_native.py",
                   "verify_production_activation_native.py", "production_sender_bootstrap_native.py"):
        assert script in runs


def test_bootstrap_uses_actual_checkout_and_original_build_evidence_without_system_cron():
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    step = next(s for s in workflow["jobs"]["native-amd64"]["steps"]
                if s.get("name") == "Build fixed sender and verify actual T0 activation")
    run = step["run"]
    assert 'source_sha="$(git rev-parse HEAD)"' in run
    assert 'Commit=${source_sha}' in run and 'revision=${source_sha}' in run
    assert 'Commit=${GITHUB_SHA}' not in run and 'revision=${GITHUB_SHA}' not in run
    assert 'clone --no-local --no-hardlinks' in run
    assert 'safe.directory=*' not in run and 'git config --global' not in run
    assert 'cp -a --no-preserve=ownership' in run
    for name in ('BUILD.log', 'IMAGE_BUILD.log', 'rawimage.json'):
        assert f'native_stage/build/{name}' in run
    assert "'--build-evidence'" in run and "sudo crontab" not in run and "sudo -n crontab" not in run


def test_host_entry_proof_is_independent_and_does_not_replace_full_activation():
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["isolated-host-ingress"]
    assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] == "5"
    assert "services" not in job and "needs" not in job
    checkout = job["steps"][0]["with"]
    assert checkout["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    assert checkout["persist-credentials"] == "false"
    run = next(s["run"] for s in job["steps"] if s.get("name") == "Independent isolated host loopback proof")
    for required in ("clone --no-local --no-hardlinks", 'install -d -m 0700 "$native_stage/source"',
                     "'--platform=linux/amd64'", "seconds=60", "time.monotonic() + 180",
                     "'--deadline-monotonic', str(deadline)", "seconds=200", "limit=512 * 1024",
                     "result['safe_evidence']['complete'] is True", "independent_synthetic_host_namespace_only",
                     "result['full_activation_verified'] is False", "result['runner_global_loopback_verified'] is False"):
        assert required in run
    assert "nginx:stable-alpine@sha256:0d3b80406a13a767339fbe2f41406d6c7da727ab89cf8fae399e81f780f814d1" in run
    for forbidden in ("go build", "verify_production_activation_native.py", "safe.directory=*", "git config --global",
                      "chmod -R", "chown", "docker system prune", "docker rm", "|| true"):
        assert forbidden not in run
    assert all("continue-on-error" not in s for s in job["steps"])
    evidence = job["steps"][-2:]
    assert all(s["if"] == "always()" for s in evidence)
    assert "prepare_synthetic_evidence.py" in evidence[0]["run"]
    assert "isolated-host-ingress=${RUNNER_TEMP}/isolated-host-ingress" in evidence[0]["run"]
    assert evidence[1]["with"]["if-no-files-found"] == "error"
    original = workflow["jobs"]["native-amd64"]
    assert "needs" not in original and original["timeout-minutes"] == "15"
    assert not any("production_host_ingress_native.py" in s.get("run", "") for s in original["steps"])
