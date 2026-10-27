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
