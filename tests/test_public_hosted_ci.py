"""Public pull requests run synthetic gates on GitHub-hosted machines only."""

import ast
from copy import deepcopy
from pathlib import Path
from fnmatch import fnmatchcase
import re
import shlex

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
EXPECTED_JOBS = {
    "ci.yml": {"lint": "ubuntu-latest", "test": "ubuntu-latest"},
    "admission-fence.yml": {"native-amd64": "ubuntu-24.04"},
    "cutover-completion.yml": {"isolated-host-ingress": "ubuntu-24.04", "native-amd64": "ubuntu-24.04",
                               "native-host-loopback131": "ubuntu-24.04"},
    "handoff-operations.yml": {"native-amd64": "ubuntu-24.04"},
    "miniflux-outbox.yml": {"fixed-source": "ubuntu-latest"},
    "miniflux-pause.yml": {"native-fixed-image": "ubuntu-latest"},
    "webhook-capacity.yml": {"native-amd64": "ubuntu-24.04"},
    "private-release-config.yml": {"native-amd64": "ubuntu-24.04"},
}
EXPECTED_TIMEOUTS = {
    "admission-fence.yml": {"native-amd64": 8},
    "cutover-completion.yml": {"isolated-host-ingress": 5, "native-amd64": 25, "native-host-loopback131": 12},
    "handoff-operations.yml": {"native-amd64": 8},
    "miniflux-outbox.yml": {"fixed-source": 15},
    "miniflux-pause.yml": {"native-fixed-image": 20},
    "webhook-capacity.yml": {"native-amd64": 40},
    "private-release-config.yml": {"native-amd64": 25},
}
POSTGRES_JOBS = {
    "miniflux-outbox.yml": "fixed-source",
    "miniflux-pause.yml": "native-fixed-image",
    "cutover-completion.yml": "native-amd64",
}


def _workflow(name):
    source = (WORKFLOWS / name).read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def test_all_existing_workflows_and_job_names_are_preserved():
    assert {path.name for path in WORKFLOWS.glob("*.yml")} == set(EXPECTED_JOBS)
    assert sum(len(jobs) for jobs in EXPECTED_JOBS.values()) == 11


def _assert_hosted_jobs(name, source, workflow):
    # PyYAML's YAML 1.1 loader treats the unquoted Actions key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert "workflow_dispatch" in triggers
    assert "pull_request" in triggers
    assert not {"pull_request_target", "workflow_run"}.intersection(triggers)
    jobs = workflow["jobs"]
    assert set(jobs) == set(EXPECTED_JOBS[name])
    for job_name, expected_runner in EXPECTED_JOBS[name].items():
        assert jobs[job_name]["runs-on"] == expected_runner
        assert "container" not in jobs[job_name]
        assert "environment" not in jobs[job_name]
        # A public fork follows the same synthetic gates; private actor/owner
        # checks must not silently skip its tests or route it to a private VM.
        if name == "ci.yml" and job_name == "test":
            assert jobs[job_name]["if"] == (
                "${{ !cancelled() && (needs.lint.result != 'success' || "
                "needs.lint.outputs.docs_only != 'true') }}"
            )
        else:
            assert "if" not in jobs[job_name]
    assert "self-hosted" not in source


@pytest.mark.parametrize("name", EXPECTED_JOBS)
def test_every_job_uses_a_literal_hosted_runner_and_can_be_dispatched(name):
    source, workflow = _workflow(name)
    _assert_hosted_jobs(name, source, workflow)


@pytest.mark.parametrize("name", EXPECTED_JOBS)
def test_public_jobs_have_read_only_permissions_and_no_persisted_git_credentials(name):
    source, workflow = _workflow(name)
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in source and "secrets[" not in source
    assert "id-token" not in source
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        checkouts = [step for step in job["steps"]
                     if step.get("uses", "").startswith("actions/checkout@")]
        assert len(checkouts) == 1
        assert checkouts[0]["with"]["persist-credentials"] is False


def _assert_timeout_contracts(name, workflow):
    expected = EXPECTED_TIMEOUTS[name]
    assert set(workflow["jobs"]) == set(expected)
    for job, minutes in expected.items():
        actual = workflow["jobs"][job]["timeout-minutes"]
        assert type(actual) is int and actual == minutes


@pytest.mark.parametrize("name", EXPECTED_TIMEOUTS)
def test_native_job_timeouts_are_not_relaxed(name):
    _, workflow = _workflow(name)
    _assert_timeout_contracts(name, workflow)


def _assert_postgres_contract(name, workflow):
    postgres = workflow["jobs"][POSTGRES_JOBS[name]]["services"]["postgres"]
    assert postgres["image"] == (
        "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
    )
    assert "--memory 384m --memory-swap 384m --cpus 1 --pids-limit 64" in postgres["options"]
    options = shlex.split(postgres["options"])
    for flag, expected in {"--memory": "384m", "--memory-swap": "384m", "--cpus": "1", "--pids-limit": "64"}.items():
        positions = [index for index, value in enumerate(options) if value.split("=", 1)[0] == flag]
        assert len(positions) == 1
        index = positions[0]
        assert options[index] == flag and options[index + 1] == expected
    assert postgres["env"] == {
        "POSTGRES_DB": "synthetic_outbox", "POSTGRES_PASSWORD": "synthetic-ci-only",
    }


@pytest.mark.parametrize("name", POSTGRES_JOBS)
def test_postgres_remains_digest_locked_and_bounded(name):
    _, workflow = _workflow(name)
    _assert_postgres_contract(name, workflow)


def _assert_isolated_ingress_contract(workflow):
    job = workflow["jobs"]["isolated-host-ingress"]
    assert job["timeout-minutes"] == 5
    assert "services" not in job
    proof = [step["run"] for step in job["steps"]
             if "scripts/production_host_ingress_native.py" in step.get("run", "")]
    assert len(proof) == 1
    blocks = re.findall(r"python -I -B - <<'PY'\n(.*?)^PY$", proof[0], re.MULTILINE | re.DOTALL)
    assert len(blocks) == 2
    nodes = [node for block in blocks for node in ast.walk(ast.parse(block))]
    deadlines = [node.value for node in nodes if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "deadline" for target in node.targets)]
    expected = ast.parse("time.monotonic() + 180", mode="eval").body
    assert len(deadlines) == 1 and ast.dump(deadlines[0]) == ast.dump(expected)
    calls = [node for node in nodes if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "archive_command"]
    assert [ast.literal_eval(next(kw.value for kw in call.keywords if kw.arg == "seconds"))
            for call in calls] == [60, 200]
    assert "'--deadline-monotonic', str(deadline)" in proof[0]


def test_independent_ingress_deadline_does_not_replace_complete_native_budget():
    _, workflow = _workflow("cutover-completion.yml")
    _assert_isolated_ingress_contract(workflow)
    _assert_timeout_contracts("cutover-completion.yml", workflow)
    _assert_postgres_contract("cutover-completion.yml", workflow)


@pytest.mark.parametrize("first", ["isolated-host-ingress", "native-amd64", "native-host-loopback131"])
def test_named_cutover_contracts_are_independent_of_job_order(first):
    name = "cutover-completion.yml"
    source, workflow = _workflow(name)
    jobs = workflow["jobs"]
    workflow["jobs"] = {first: jobs[first], **{key: value for key, value in jobs.items() if key != first}}
    _assert_hosted_jobs(name, source, workflow)
    _assert_timeout_contracts(name, workflow)
    _assert_postgres_contract(name, workflow)
    _assert_isolated_ingress_contract(workflow)


@pytest.mark.parametrize("mutation", ["extra", "missing-original", "missing-independent", "missing-host131", "self-hosted",
                                     "dynamic-runner", "container", "environment", "skip"])
def test_cutover_job_contract_rejects_unapproved_jobs_and_execution_paths(mutation):
    name = "cutover-completion.yml"
    source, original = _workflow(name)
    workflow = deepcopy(original)
    jobs, isolated = workflow["jobs"], workflow["jobs"]["isolated-host-ingress"]
    if mutation == "extra":
        jobs["unexpected"] = deepcopy(isolated)
    elif mutation == "missing-original":
        del jobs["native-amd64"]
    elif mutation == "missing-independent":
        del jobs["isolated-host-ingress"]
    elif mutation == "missing-host131":
        del jobs["native-host-loopback131"]
    elif mutation == "self-hosted":
        isolated["runs-on"] = ["self-hosted", "linux"]
    elif mutation == "dynamic-runner":
        isolated["runs-on"] = "${{ inputs.runner }}"
    elif mutation == "container":
        isolated["container"] = "synthetic:latest"
    elif mutation == "environment":
        isolated["environment"] = "production"
    else:
        isolated["if"] = "${{ false }}"
    with pytest.raises(AssertionError):
        _assert_hosted_jobs(name, source, workflow)


@pytest.mark.parametrize("job", ["native-amd64", "isolated-host-ingress", "native-host-loopback131"])
@pytest.mark.parametrize("replacement", [1, 15, 16, 24, 26, "15", "25", None, 5.0, 15.0, 25.0])
def test_cutover_each_named_timeout_is_exact(job, replacement):
    _, workflow = _workflow("cutover-completion.yml")
    workflow["jobs"][job]["timeout-minutes"] = replacement
    with pytest.raises(AssertionError):
        _assert_timeout_contracts("cutover-completion.yml", workflow)


@pytest.mark.parametrize("field,replacement", [
    ("image", "postgres:latest"),
    ("options", "--memory 768m --memory-swap 768m --cpus 1 --pids-limit 128"),
    ("options", "--memory 384m --memory-swap 768m --cpus 1 --pids-limit 64"),
    ("options", "--memory 384m --memory-swap 384m --cpus 2 --pids-limit 64"),
    ("options", "--memory 384m --memory-swap 384m --cpus 1 --pids-limit 128"),
    ("env", {"POSTGRES_DB": "other", "POSTGRES_PASSWORD": "synthetic-ci-only"}),
])
def test_named_native_postgres_contract_still_rejects_drift(field, replacement):
    _, workflow = _workflow("cutover-completion.yml")
    workflow["jobs"]["native-amd64"]["services"]["postgres"][field] = replacement
    with pytest.raises(AssertionError):
        _assert_postgres_contract("cutover-completion.yml", workflow)


@pytest.mark.parametrize("override", ["--memory 768m", "--memory=768m", "--memory-swap 768m",
                                      "--cpus=2", "--pids-limit 128"])
def test_postgres_cannot_override_exact_resource_flags_with_later_duplicates(override):
    _, workflow = _workflow("cutover-completion.yml")
    postgres = workflow["jobs"]["native-amd64"]["services"]["postgres"]
    postgres["options"] += " " + override
    with pytest.raises(AssertionError):
        _assert_postgres_contract("cutover-completion.yml", workflow)


@pytest.mark.parametrize("before,after", [
    ("deadline = time.monotonic() + 180", "deadline = time.monotonic() + 181"),
    ("deadline = time.monotonic() + 180", "deadline = time.monotonic() + int(os.environ['BUDGET'])"),
    ("seconds=200", "seconds=201"),
    ("seconds=60", "seconds=61"),
    ("'--deadline-monotonic', str(deadline)", "'--deadline-monotonic', str(deadline + 60)"),
])
def test_independent_ingress_deadline_and_transfer_limits_reject_drift(before, after):
    _, workflow = _workflow("cutover-completion.yml")
    step = next(step for step in workflow["jobs"]["isolated-host-ingress"]["steps"]
                if "scripts/production_host_ingress_native.py" in step.get("run", ""))
    assert before in step["run"]
    step["run"] = step["run"].replace(before, after)
    with pytest.raises(AssertionError):
        _assert_isolated_ingress_contract(workflow)


def test_independent_ingress_cannot_add_an_unbudgeted_postgres_service():
    _, workflow = _workflow("cutover-completion.yml")
    workflow["jobs"]["isolated-host-ingress"]["services"] = deepcopy(
        workflow["jobs"]["native-amd64"]["services"])
    with pytest.raises(AssertionError):
        _assert_isolated_ingress_contract(workflow)


@pytest.mark.parametrize("name", EXPECTED_JOBS)
def test_only_superseded_pull_requests_cancel_previous_runs(name):
    _, workflow = _workflow(name)
    concurrency = workflow["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "github.run_id" in concurrency["group"]
    assert "format('pr-{0}', github.event.pull_request.number)" in concurrency["group"]


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("changed_path,expected", [
    ("tests/test_private_release_contract.py", True),
    ("tests/test_private_release_native.py", True),
    ("docs/private-release-config.md", False),
    ("tests/test_unrelated_feature.py", False),
])
def test_private_release_workflow_covers_its_tests_without_unrelated_paths(event, changed_path, expected):
    _, workflow = _workflow("private-release-config.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert any(fnmatchcase(changed_path, pattern) for pattern in triggers[event]["paths"]) is expected


@pytest.mark.parametrize("workflow,changed_path,expected", [
    ("cutover-completion.yml", "services/production_startup.py", True),
    ("cutover-completion.yml", "db/startup_migration_steps.py", True),
    ("cutover-completion.yml", "db/database.py", True),
    ("cutover-completion.yml", "gunicorn.conf.py", True),
    ("cutover-completion.yml", "services/receiver_drain_transfer.py", True),
    ("cutover-completion.yml", "services/sender_drain_contract.py", True),
    ("cutover-completion.yml", "services/admission_http.py", True),
    ("cutover-completion.yml", "routes/webhook_routes.py", True),
    ("cutover-completion.yml", "requirements-ops.txt", True),
    ("cutover-completion.yml", "Dockerfile", True),
    ("cutover-completion.yml", "requirements.txt", True),
    ("cutover-completion.yml", "requirements-prod.txt", True),
    ("cutover-completion.yml", "start.sh", True),
    ("cutover-completion.yml", "config.yaml", True),
    ("cutover-completion.yml", "scripts/production_startup_pg_fixture.py", True),
    ("cutover-completion.yml", "scripts/production_startup_fixture.py", True),
    ("cutover-completion.yml", "scripts/production_sender_bootstrap_host_native.py", True),
    ("cutover-completion.yml", "tests/test_production_sender_bootstrap_host_native.py", True),
    ("cutover-completion.yml", "scripts/production_sender_bootstrap_host_native.py.bak", False),
    ("cutover-completion.yml", "scripts/nested/production_sender_bootstrap_host_native.py", False),
    ("cutover-completion.yml", "scripts/private_release_contract.py", True),
    ("cutover-completion.yml", "scripts/check_release_container_lifecycle.py", True),
    ("cutover-completion.yml", "scripts/release.sh", True),
    ("cutover-completion.yml", "tests/test_release_container_lifecycle.py", True),
    ("cutover-completion.yml", "tests/test_miniflux_activation_adapter.py", True),
    ("cutover-completion.yml", "tests/test_sender_activation_driver.py", True),
    ("cutover-completion.yml", "scripts/prepare_synthetic_evidence.py", True),
    ("cutover-completion.yml", "tests/test_prepare_synthetic_evidence.py", True),
    ("cutover-completion.yml", "ops/miniflux/pause-source-lock.json", True),
    ("cutover-completion.yml", "tests/test_sender_pg_deadlines.py", True),
    ("cutover-completion.yml", "scripts/verify_production_startup_native.py", True),
    ("cutover-completion.yml", "scripts/verify_production_activation_native.py", True),
    ("cutover-completion.yml", "scripts/production_activation_fixture.py", True),
    ("cutover-completion.yml", "services/production_container_lifecycle.py", True),
    ("cutover-completion.yml", "services/production_activation_control.py", True),
    ("cutover-completion.yml", "tests/test_production_activation_native.py", True),
    ("cutover-completion.yml", "tests/test_production_initial_migration.py", True),
    ("cutover-completion.yml", "scripts/production_activation_notes.md", False),
    ("cutover-completion.yml", "tests/test_production_startup.py", True),
    ("cutover-completion.yml", "tests/test_production_startup_native.py", True),
    ("cutover-completion.yml", "tests/test_joint_drain_controller.py", True),
    ("cutover-completion.yml", "tests/test_takeover_readonly_preflight.py", True),
    ("cutover-completion.yml", "docs/release-notes.md", False),
    ("cutover-completion.yml", "tests/test_numeric_facts.py", False),
    ("cutover-completion.yml", "requirements-dev.txt", False),
    ("cutover-completion.yml", "start.sh.example", False),
    ("cutover-completion.yml", "scripts/private_release_contract_notes.md", False),
    ("cutover-completion.yml", "scripts/prepare_synthetic_evidence_notes.md", False),
    ("miniflux-pause.yml", "scripts/production_sender_driver.py", True),
    ("miniflux-pause.yml", "scripts/sender_drain_inventory.py", True),
    ("miniflux-pause.yml", "services/sender_drain_contract.py", True),
    ("miniflux-pause.yml", "services/admission_fence.py", True),
    ("miniflux-pause.yml", "tests/test_production_sender_driver.py", True),
    ("miniflux-pause.yml", "tests/test_sender_drain_contract.py", True),
    ("miniflux-pause.yml", "tests/test_sender_postgres_native.py", True),
    ("miniflux-pause.yml", "tests/test_sender_pg_deadlines.py", True),
    ("miniflux-pause.yml", "scripts/production_startup_pg_fixture.py", True),
    ("miniflux-pause.yml", "tests/test_production_startup_pg_fixture.py", True),
    ("miniflux-pause.yml", "scripts/verify_sender_receiver_joint_native.py", True),
    ("miniflux-pause.yml", "scripts/receiver_drain_fixture_runtime.py", True),
    ("miniflux-pause.yml", "routes/webhook_routes.py", True),
    ("miniflux-pause.yml", "Dockerfile", True),
    ("miniflux-pause.yml", "requirements-ops.txt", True),
    ("miniflux-pause.yml", "docs/release-notes.md", False),
    ("miniflux-pause.yml", "tests/test_numeric_facts.py", False),
    ("miniflux-pause.yml", "services/admission_fence_notes.md", False),
])
def test_combined_cutover_regressions_trigger_the_relevant_native_gate(workflow, changed_path, expected):
    _, value = _workflow(workflow)
    triggers = value.get("on", value.get(True))
    for event in ("pull_request", "push"):
        assert any(fnmatchcase(changed_path, pattern) for pattern in triggers[event]["paths"]) is expected


@pytest.mark.parametrize('name,job,upload_name,destination', [
    ('ci.yml', 'test', 'Preserve isolated synthetic cutover evidence', 'ai-filter-synthetic-upload'),
    ('cutover-completion.yml', 'native-amd64', 'Preserve original evidence including failures', 'cutover-safe-upload'),
])
def test_synthetic_uploads_only_use_prepared_link_free_evidence_even_on_failure(name, job, upload_name, destination):
    _, workflow = _workflow(name)
    steps = workflow['jobs'][job]['steps']
    prepared = [step for step in steps if 'scripts/prepare_synthetic_evidence.py' in step.get('run', '')]
    assert len(prepared) == 1 and prepared[0]['if'] == 'always()'
    upload = next(step for step in steps if step.get('name') == upload_name)
    assert steps.index(prepared[0]) < steps.index(upload) and upload['if'] == 'always()'
    assert upload['with']['path'] == '${{ runner.temp }}/' + destination + '/'
    assert upload['with']['include-hidden-files'] is True and upload['with']['if-no-files-found'] == 'error'
    assert '--output "${RUNNER_TEMP}/' + destination + '"' in prepared[0]['run']


def test_host_postgres_driver_is_pinned_without_changing_application_dependencies():
    root = WORKFLOWS.parents[1]
    pins = {line for line in (root / "requirements-ops.txt").read_text().splitlines()
            if line and not line.startswith("#")}
    assert pins == {"psycopg==3.3.6", "psycopg-binary==3.3.6"}
    for name in ("requirements-prod.txt", "requirements.txt", "Dockerfile"):
        text = (root / name).read_text()
        assert "requirements-ops" not in text and "psycopg" not in text
    for name in ("cutover-completion.yml", "miniflux-pause.yml"):
        source, _ = _workflow(name)
        assert "--only-binary=:all: -r requirements.txt -r requirements-ops.txt" in source


def _assert_main_ops_install(workflow):
    steps = workflow["jobs"]["test"]["steps"]
    installs = [step for step in steps if step.get("name") == "Install dependencies"]
    assert len(installs) == 1 and "if" not in installs[0]
    assert [shlex.split(line) for line in installs[0]["run"].splitlines() if line.strip()] == [
        ["python", "-m", "pip", "install", "--upgrade", "pip"],
        ["pip", "install", "-r", "requirements.txt"],
        ["pip", "install", "--only-binary=:all:", "-r", "requirements-ops.txt"],
    ]
    caches = [step for step in steps if step.get("name") == "Cache pip dependencies"]
    assert len(caches) == 1 and caches[0]["with"]["key"] == (
        "${{ runner.os }}-pip-${{ hashFiles('requirements.txt', 'requirements-prod.txt', 'requirements-ops.txt') }}"
    )
    test_index = next(index for index, step in enumerate(steps)
                      if step.get("name") == "Run isolated full test suite with coverage")
    assert steps.index(installs[0]) < test_index


def test_main_ci_really_installs_pinned_ops_driver_before_the_complete_suite():
    _, workflow = _workflow("ci.yml")
    _assert_main_ops_install(workflow)


@pytest.mark.parametrize("mutation", ["missing-ops", "source-build", "missing-app", "comment-only",
                                     "conditional", "after-tests", "stale-cache"])
def test_main_ci_ops_install_cannot_be_skipped_or_replaced(mutation):
    _, workflow = _workflow("ci.yml")
    steps = workflow["jobs"]["test"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install dependencies")
    if mutation == "missing-ops":
        install["run"] = install["run"].replace("pip install --only-binary=:all: -r requirements-ops.txt", "")
    elif mutation == "source-build":
        install["run"] = install["run"].replace(" --only-binary=:all:", "")
    elif mutation == "missing-app":
        install["run"] = install["run"].replace("pip install -r requirements.txt", "")
    elif mutation == "comment-only":
        install["run"] = install["run"].replace("pip install --only-binary=:all:", "# pip install --only-binary=:all:")
    elif mutation == "conditional":
        install["if"] = "${{ false }}"
    elif mutation == "after-tests":
        steps.remove(install)
        steps.append(install)
    else:
        next(step for step in steps if step.get("name") == "Cache pip dependencies")["with"]["key"] = (
            "${{ runner.os }}-pip-${{ hashFiles('requirements.txt', 'requirements-prod.txt') }}"
        )
    with pytest.raises(AssertionError):
        _assert_main_ops_install(workflow)


def test_frozen_cutover_merge_push_is_master_only_and_preserves_pr_scope():
    _, workflow = _workflow("cutover-completion.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["branches"] == ["master"]
    assert triggers["pull_request"]["branches"] == ["master", "codex/**"]
    assert triggers["push"]["paths"] == triggers["pull_request"]["paths"]
    assert "tags" not in triggers["push"]
    for branch, expected in (("master", True), ("codex/activation", False), ("feature/activation", False), ("main", False)):
        assert any(fnmatchcase(branch, pattern) for pattern in triggers["push"]["branches"]) is expected


def test_activation_native_runs_after_closed_startup_and_preserves_failures():
    _, workflow = _workflow("cutover-completion.yml")
    steps = workflow["jobs"]["native-amd64"]["steps"]
    closed = next(i for i, step in enumerate(steps) if "scripts/verify_production_startup_native.py" in step.get("run", ""))
    activation = next(i for i, step in enumerate(steps) if "scripts/production_activation_host_native.py" in step.get("run", ""))
    assert closed < activation
    run = steps[activation]["run"]
    assert "'--source', str(root / 'miniflux')" in run
    assert "CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -p 1 -trimpath" in run
    assert 'chmod 0755 "$RUNNER_TEMP/production-startup-source/production-activation-build/miniflux"' in run
    assert "redis:7-alpine@sha256:" in run and "--platform=linux/amd64" in run
    prepare = next(step for step in steps if "scripts/prepare_synthetic_evidence.py" in step.get("run", ""))
    assert prepare["if"] == "always()"
    assert "'--output', str(root / 'activation-originals')" in run
    assert "'production-startup-source' / 'ACTIVATION_HOST_PROCESS'" in run
    assert "result['full_activation_verified'] is True" in run
    assert "seconds=600" in run and "limit=512 * 1024" in run
    assert prepare["run"].count("--source ") == 4
    assert '--source "production-startup-source=${RUNNER_TEMP}/production-startup-source"' in prepare["run"]
    assert '--source "production-activation' not in prepare["run"]


def test_activation_nested_evidence_fits_real_four_source_preparer(tmp_path):
    from scripts import prepare_synthetic_evidence as evidence
    _, workflow = _workflow("cutover-completion.yml")
    steps = workflow["jobs"]["native-amd64"]["steps"]
    prepare = next(step["run"] for step in steps if "scripts/prepare_synthetic_evidence.py" in step.get("run", ""))
    pairs = re.findall(r'--source "([a-z-]+)=\$\{RUNNER_TEMP\}/([a-z-]+)"', prepare)
    sources = {label: tmp_path / name for label, name in pairs}
    assert len(pairs) == len(sources) == 4
    for path in sources.values():
        path.mkdir(mode=0o700)
    for name in ("production-activation", "production-activation-build"):
        directory = sources["production-startup-source"] / name
        directory.mkdir(mode=0o700)
        original = directory / "original.json"
        original.write_bytes(b'{"synthetic":true}')
        original.chmod(0o600)
    result = evidence.prepare(sources, tmp_path / "upload")
    assert result["complete"] and not result["errors"]
    assert len(result["files"]) == 2
    assert evidence.MAX_BYTES == 8 * 1024**3 and evidence.MAX_ENTRIES == 50_000
    assert evidence.MAX_METADATA == 8 * 1024**2 and evidence.MAX_OUTPUT_METADATA == 32 * 1024**2
    for name in ("production-activation", "production-activation-build"):
        assert (tmp_path / "upload/files/production-startup-source" / name / "original.json").read_bytes() == b'{"synthetic":true}'
    expanded = {**sources, "production-activation": tmp_path / "legacy-extra-1",
                "production-activation-build": tmp_path / "legacy-extra-2"}
    with pytest.raises(evidence.EvidenceError, match="source_count_bound"):
        evidence.prepare(expanded, tmp_path / "six-root-output")
    assert not (tmp_path / "six-root-output").exists()
