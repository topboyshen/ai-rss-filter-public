"""Every adapter change selects the real capped native suite; no broad exemption."""
import ast
from copy import deepcopy
from pathlib import Path
import re
import shlex

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
    "scripts/production_activation_host_native.py", "tests/test_production_activation_host_native.py",
    "scripts/production_offline_registry.py", "tests/test_production_offline_registry.py",
    "scripts/production_native_deadline.py", "tests/test_production_native_deadline.py",
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
    "scripts/production_activation_host_native.py.bak", "scripts/nested/production_activation_host_native.py",
    "scripts/production_offline_registry.py.bak", "scripts/nested/production_offline_registry.py",
    "scripts/production_native_deadline.py.bak", "scripts/nested/production_native_deadline.py",
    "tests/nested/test_controlled_release_finalize.py", "tests/test_unrelated.py",
])
def test_unrelated_and_lookalike_paths_do_not_trigger(event, path):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    assert not selected(path, workflow["on"][event]["paths"])


@pytest.mark.parametrize("workflow_name", ["cutover-completion.yml", "private-release-config.yml"])
@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("path,expected", [
    ("scripts/production_native_deadline.py", True), ("tests/test_production_native_deadline.py", True),
    ("scripts/production_native_deadline.py.bak", False), ("scripts/nested/production_native_deadline.py", False),
    ("tests/test_production_native_deadline.py.bak", False), ("tests/nested/test_production_native_deadline.py", False),
    ("scripts/production_native_deadline_notes.py", False), ("docs/production-native-deadline.md", False),
])
def test_shared_native_deadline_selects_both_consumers_without_expanding_scope(workflow_name, event, path, expected):
    workflow = yaml.load((ROOT / ".github/workflows" / workflow_name).read_text(), Loader=yaml.BaseLoader)
    assert selected(path, workflow["on"][event]["paths"]) is expected


def test_original_resources_and_complete_native_steps_remain():
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["native-amd64"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "25"
    pg = job["services"]["postgres"]["options"]
    for token in ("--memory 384m", "--memory-swap 384m", "--pids-limit 64"):
        assert token in pg
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    for script in ("verify_cutover_completion_native.py", "verify_production_startup_native.py",
                   "production_activation_host_native.py", "production_sender_bootstrap_native.py"):
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
    assert "needs" not in original and original["timeout-minutes"] == "25"
    assert not any("production_host_ingress_native.py" in s.get("run", "") for s in original["steps"])


def test_full_activation_uses_private_clone_and_real_result_without_guard_overrides():
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    job = workflow['jobs']['native-amd64']
    run = _full_activation_step(workflow)['run']
    for required in ("'source/scripts/production_activation_host_native.py'", "'/usr/bin/sudo', '-n', '--'",
                     "'--repo', str(root / 'source')", "'--source', str(root / 'miniflux')",
                     "'--binary', str(root / 'miniflux-binary')", "'--output', str(root / 'activation-originals')",
                     "'ACTIVATION_HOST_PROCESS'", "seconds=600", "limit=512 * 1024", "child_umask=0o077",
                     "result['status'] == 'passed'", "result['safe_evidence']['complete'] is True",
                     "result['full_activation_verified'] is True"):
        assert required in run
    for forbidden in ('|| true', 'continue-on-error', '--privileged', '--network=host', 'docker exec',
                      'skip-guard', 'allow-unsafe', 'safe.directory=*', 'chmod -R', 'chown -R'):
        assert forbidden not in run
    assert job['timeout-minutes'] == '25'
    assert all('continue-on-error' not in step for step in job['steps'])
    collector = next(step['run'] for step in job['steps'] if step.get('name') == 'Prepare evidence without following fixture links')
    assert 'production-startup-source=${RUNNER_TEMP}/production-startup-source' in collector
    _assert_full_activation_contract(run)
    _assert_private_clone_contract(run)


def _full_activation_step(workflow):
    steps = [step for step in workflow["jobs"]["native-amd64"]["steps"]
             if step.get("name") == "Build fixed sender and verify actual T0 activation"]
    assert len(steps) == 1 and "if" not in steps[0] and "continue-on-error" not in steps[0]
    return steps[0]


def _same_expression(actual, expected):
    assert ast.dump(actual, include_attributes=False) == ast.dump(ast.parse(expected, mode="eval").body, include_attributes=False)


def _single_assignment(tree, name):
    values = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
              and node.id == name and isinstance(node.ctx, ast.Store)]
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name]
    assert len(values) == len(assignments) == 1
    return assignments[0]


def _full_activation_tree(run):
    blocks = re.findall(r"python -I -B - <<'PY'\n(.*?)^PY$", run, re.MULTILINE | re.DOTALL)
    trees = [ast.parse(block) for block in blocks]
    selected = [tree for tree in trees if any(isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "output" for target in node.targets)
                and any(isinstance(value, ast.Constant) and value.value == "ACTIVATION_HOST_PROCESS"
                        for value in ast.walk(node.value)) for node in tree.body)]
    assert len(selected) == 1
    return selected[0]


def _assert_full_activation_contract(run):
    tree = _full_activation_tree(run)
    root, output, args, result = [_single_assignment(tree, name) for name in ("root", "output", "args", "result")]
    _same_expression(root.value, "pathlib.Path(os.environ['AI_FILTER_NATIVE_BOOTSTRAP_STAGE'])")
    _same_expression(output.value, "pathlib.Path(os.environ['RUNNER_TEMP']) / 'production-startup-source' / 'ACTIVATION_HOST_PROCESS'")
    _same_expression(args.value, "['/usr/bin/sudo', '-n', '--', sys.executable, '-I', '-B', "
        "str(root / 'source/scripts/production_activation_host_native.py'), '--repo', str(root / 'source'), "
        "'--source', str(root / 'miniflux'), '--binary', str(root / 'miniflux-binary'), "
        "'--image-id', os.environ['AI_FILTER_NATIVE_BOOTSTRAP_IMAGE'], '--output', str(root / 'activation-originals')]")
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "archive" and node.func.attr == "archive_command"]
    assert len(calls) == 1 and any(isinstance(node, ast.Expr) and node.value is calls[0] for node in tree.body)
    call = calls[0]
    assert len(call.args) == 2 and len(call.keywords) == 3
    _same_expression(call.args[0], "args")
    _same_expression(call.args[1], "output")
    assert {keyword.arg for keyword in call.keywords} == {"seconds", "limit", "child_umask"}
    for keyword in call.keywords:
        _same_expression(keyword.value, {"seconds": "600", "limit": "512 * 1024", "child_umask": "0o077"}[keyword.arg])
    _same_expression(result.value, "json.loads(pathlib.Path(str(output) + '.stdout').read_bytes())")
    assertions = [node for node in tree.body if isinstance(node, ast.Assert)]
    assert len(assertions) == 2 and all(node.msg is None for node in assertions)
    _same_expression(assertions[0].test, "result['status'] == 'passed' and result['safe_evidence']['complete'] is True")
    _same_expression(assertions[1].test, "result['full_activation_verified'] is True")
    assert root.lineno < output.lineno < args.lineno < call.lineno < result.lineno < assertions[0].lineno < assertions[1].lineno


def _assert_private_clone_contract(run):
    commands = [shlex.split(line, comments=True) for line in run.replace("\\\n", " ").splitlines()]
    clones = [tokens for tokens in commands if tokens[:3] == ["sudo", "-n", "git"] and "clone" in tokens]
    assert clones == [["sudo", "-n", "git", "-c", "safe.directory=$GITHUB_WORKSPACE", "clone", "--no-local", "--no-hardlinks",
                       "$GITHUB_WORKSPACE", "$native_stage/source"]]
    checks = [tokens for tokens in commands if tokens[:3] == ["sudo", "-n", "test"]]
    assert checks == [["sudo", "-n", "test", "$(sudo -n git -C $native_stage/source rev-parse HEAD)", "=", "$source_sha"]]


@pytest.mark.parametrize("before,after", [
    ("seconds=600", "seconds=601"), ("limit=512 * 1024", "limit=513 * 1024"),
    ("limit=512 * 1024", "limit=512 * 1024 + 1"), ("child_umask=0o077", "child_umask=0o022"),
    ("'/usr/bin/sudo'", "'/bin/true'"),
    ("source/scripts/production_activation_host_native.py", "source/scripts/production_host_ingress_native.py"),
    ("'--source', str(root / 'miniflux')", "'--source', str(root / 'other')"),
    ("'--output', str(root / 'activation-originals')", "'--output', str(root / 'other')"),
    ("'ACTIVATION_HOST_PROCESS'", "'OTHER_PROCESS'"),
    ("str(output) + '.stdout'", "str(output) + '.stderr'"),
    ("result['safe_evidence']['complete'] is True", "result['safe_evidence']['complete'] is False"),
    ("result['full_activation_verified'] is True", "result['full_activation_verified'] is False"),
])
def test_full_activation_ast_rejects_actual_drift_with_expected_text_left_as_decoy(before, after):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    run = next(step["run"] for step in workflow["jobs"]["native-amd64"]["steps"]
               if step.get("name") == "Build fixed sender and verify actual T0 activation")
    block = next(block for block in re.findall(r"python -I -B - <<'PY'\n(.*?)^PY$", run, re.MULTILINE | re.DOTALL)
                 if "'ACTIVATION_HOST_PROCESS'" in block)
    assert before in block
    changed = run.replace(block, block.replace(before, after, 1) + "# expected-text-decoy: " + before + "\n", 1)
    assert before in changed  # String-presence checks alone would still find the frozen text.
    with pytest.raises(AssertionError):
        _assert_full_activation_contract(changed)


@pytest.mark.parametrize("mutation", ["missing_call", "duplicate_call", "mutated_args", "conditional_call"])
def test_full_activation_ast_requires_exactly_one_real_unconditional_call(mutation):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    run = next(step["run"] for step in workflow["jobs"]["native-amd64"]["steps"]
               if step.get("name") == "Build fixed sender and verify actual T0 activation")
    call = "archive.archive_command(args, output, seconds=600, limit=512 * 1024, child_umask=0o077)"
    replacement = {"missing_call": "# " + call, "duplicate_call": call + "\n" + call,
                   "mutated_args": "args += ['--skip-guard']\n" + call,
                   "conditional_call": "if False:\n    " + call}[mutation]
    assert call in run
    with pytest.raises(AssertionError):
        _assert_full_activation_contract(run.replace(call, replacement, 1))


@pytest.mark.parametrize("before,after", [
    ("clone --no-local --no-hardlinks", "clone --no-hardlinks"),
    ("clone --no-local --no-hardlinks", "clone --no-local"),
    ('"safe.directory=$GITHUB_WORKSPACE"', '"safe.directory=*"'),
    ('"$GITHUB_WORKSPACE" "$native_stage/source"', '"$GITHUB_WORKSPACE" "$native_stage/other"'),
    ('= "$source_sha"', '= "$GITHUB_SHA"'),
])
def test_full_root_clone_contract_rejects_drift_even_with_expected_comment(before, after):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    run = next(step["run"] for step in workflow["jobs"]["native-amd64"]["steps"]
               if step.get("name") == "Build fixed sender and verify actual T0 activation")
    assert before in run
    changed = run.replace(before, after, 1) + "\n# expected-clone-decoy: " + before + "\n"
    assert before in changed
    with pytest.raises(AssertionError):
        _assert_private_clone_contract(changed)


@pytest.mark.parametrize("field,value", [("if", "${{ false }}"), ("if", "always()"), ("continue-on-error", "true")])
def test_full_activation_step_cannot_skip_or_ignore_the_original_gate(field, value):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    _full_activation_step(workflow)[field] = value
    with pytest.raises(AssertionError):
        _full_activation_step(workflow)


PRIORITY_UPLOAD_NAME = "Preserve bounded prepared priority diagnostics"
PRIORITY_FILES = (
    "PRIORITY_DIAGNOSTICS.json",
    "files/production-startup-source/BOOTSTRAP_PROCESS.stdout",
    "files/production-startup-source/BOOTSTRAP_PROCESS.stderr",
    "files/production-startup-source/ACTIVATION_HOST_PROCESS.stdout",
    "files/production-startup-source/ACTIVATION_HOST_PROCESS.stderr",
)
PREPARED_ROOT = "${{ runner.temp }}/cutover-safe-upload/"


def _named_step(steps, name):
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _assert_priority_upload_contract(workflow):
    steps = workflow["jobs"]["native-amd64"]["steps"]
    prepare = _named_step(steps, "Prepare evidence without following fixture links")
    small = _named_step(steps, PRIORITY_UPLOAD_NAME)
    original = _named_step(steps, "Preserve original evidence including failures")
    assert steps.index(small) == steps.index(prepare) + 1
    assert steps.index(original) == steps.index(small) + 1
    assert set(prepare) == {"name", "if", "run"} and prepare["if"] == "always()"
    actual = shlex.split(prepare["run"].replace("\\\n", " "), comments=True)
    expected = ["python", "-I", "-B", "scripts/prepare_synthetic_evidence.py"]
    for source in ("cutover-completion", "production-startup", "production-startup-postgres", "production-startup-source"):
        expected.extend(("--source", source + "=${RUNNER_TEMP}/" + source))
    expected.extend(("--output", "${RUNNER_TEMP}/cutover-safe-upload"))
    assert actual == expected
    for step, name, paths in (
        (small, "cutover-priority-diagnostics-${{ github.run_id }}-${{ github.run_attempt }}",
         [PREPARED_ROOT + path for path in PRIORITY_FILES]),
        (original, "cutover-completion-${{ github.run_id }}-${{ github.run_attempt }}", [PREPARED_ROOT]),
    ):
        assert set(step) == {"name", "if", "uses", "with"} and step["if"] == "always()"
        assert step["uses"] == "actions/upload-artifact@v7"
        config = step["with"]
        assert set(config) == {"name", "path", "include-hidden-files", "if-no-files-found", "retention-days"}
        assert config["name"] == name and config["path"].splitlines() == paths
        assert config["include-hidden-files"] == "true" and config["if-no-files-found"] == "error"
        assert config["retention-days"] == "14"


def test_small_priority_artifact_uses_only_five_exact_prepared_paths_before_unchanged_full_archive():
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    _assert_priority_upload_contract(workflow)


@pytest.mark.parametrize("removed", PRIORITY_FILES)
def test_priority_artifact_cannot_omit_any_fixed_log_or_result_state(removed):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    step = _named_step(workflow["jobs"]["native-amd64"]["steps"], PRIORITY_UPLOAD_NAME)
    step["with"]["path"] = "\n".join(path for path in step["with"]["path"].splitlines()
                                        if path != PREPARED_ROOT + removed)
    with pytest.raises(AssertionError):
        _assert_priority_upload_contract(workflow)


@pytest.mark.parametrize("path", [
    "${{ runner.temp }}/production-startup-source/BOOTSTRAP_PROCESS.stdout",
    "${{ runner.temp }}/cutover-safe-upload/files/production-startup-source/",
    "${{ runner.temp }}/cutover-safe-upload/**",
    "${{ runner.temp }}/cutover-safe-upload/priority-diagnostics/",
    "${{ runner.temp }}/production-native-bootstrap/activation-originals/",
    "/root/activation-originals/", "", PREPARED_ROOT,
])
def test_priority_artifact_cannot_upload_raw_parent_glob_or_unprepared_paths(path):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    step = _named_step(workflow["jobs"]["native-amd64"]["steps"], PRIORITY_UPLOAD_NAME)
    step["with"]["path"] = path
    with pytest.raises(AssertionError):
        _assert_priority_upload_contract(workflow)


@pytest.mark.parametrize("field,value", [
    ("if", "success()"), ("if", "${{ false }}"), ("continue-on-error", "true"),
    ("uses", "actions/upload-artifact@v6"), ("env", {"RAW_SOURCE": "/root/private"}),
])
def test_priority_artifact_cannot_skip_failed_runs_or_ignore_upload_errors(field, value):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    _named_step(workflow["jobs"]["native-amd64"]["steps"], PRIORITY_UPLOAD_NAME)[field] = value
    with pytest.raises(AssertionError):
        _assert_priority_upload_contract(workflow)


@pytest.mark.parametrize("field,value", [
    ("if-no-files-found", "warn"), ("if-no-files-found", "ignore"),
    ("include-hidden-files", "false"), ("retention-days", "15"),
    ("name", "cutover-completion-${{ github.run_id }}-${{ github.run_attempt }}"),
])
def test_priority_artifact_rejects_missing_file_lenience_or_archive_identity_drift(field, value):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    _named_step(workflow["jobs"]["native-amd64"]["steps"], PRIORITY_UPLOAD_NAME)["with"][field] = value
    with pytest.raises(AssertionError):
        _assert_priority_upload_contract(workflow)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "before-collector", "after-original", "interposed-write"])
def test_priority_artifact_requires_one_upload_between_collector_and_full_archive(mutation):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["native-amd64"]["steps"]
    small = _named_step(steps, PRIORITY_UPLOAD_NAME)
    if mutation == "duplicate":
        steps.append(deepcopy(small))
    elif mutation == "interposed-write":
        steps.insert(steps.index(small), {"name": "Unverified overwrite", "run": "cp raw-source prepared-output"})
    else:
        steps.remove(small)
        if mutation == "before-collector":
            steps.insert(0, small)
        elif mutation == "after-original":
            steps.append(small)
    with pytest.raises(AssertionError):
        _assert_priority_upload_contract(workflow)


@pytest.mark.parametrize("mutation", ["missing-source", "extra-source", "raw-output", "ignored-error", "skip"])
def test_priority_upload_preserves_real_four_source_collector_without_bypass(mutation):
    workflow = yaml.load((ROOT / PATH).read_text(), Loader=yaml.BaseLoader)
    prepare = _named_step(workflow["jobs"]["native-amd64"]["steps"], "Prepare evidence without following fixture links")
    if mutation == "missing-source":
        prepare["run"] = prepare["run"].replace('--source "production-startup=${RUNNER_TEMP}/production-startup"', "")
    elif mutation == "extra-source":
        prepare["run"] += ' --source "private=/root/private"'
    elif mutation == "raw-output":
        prepare["run"] = prepare["run"].replace('--output "${RUNNER_TEMP}/cutover-safe-upload"',
                                                '--output "${RUNNER_TEMP}/production-startup-source"')
    elif mutation == "ignored-error":
        prepare["run"] += " || true"
    else:
        prepare["if"] = "success()"
    with pytest.raises(AssertionError):
        _assert_priority_upload_contract(workflow)
