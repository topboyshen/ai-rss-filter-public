"""Public pull requests run synthetic gates on GitHub-hosted machines only."""

from pathlib import Path
from fnmatch import fnmatchcase

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
EXPECTED_JOBS = {
    "ci.yml": {"lint": "ubuntu-latest", "test": "ubuntu-latest"},
    "admission-fence.yml": {"native-amd64": "ubuntu-24.04"},
    "cutover-completion.yml": {"native-amd64": "ubuntu-24.04"},
    "handoff-operations.yml": {"native-amd64": "ubuntu-24.04"},
    "miniflux-outbox.yml": {"fixed-source": "ubuntu-latest"},
    "miniflux-pause.yml": {"native-fixed-image": "ubuntu-latest"},
    "webhook-capacity.yml": {"native-amd64": "ubuntu-24.04"},
    "private-release-config.yml": {"native-amd64": "ubuntu-24.04"},
}
EXPECTED_TIMEOUTS = {
    "admission-fence.yml": 8,
    "cutover-completion.yml": 15,
    "handoff-operations.yml": 8,
    "miniflux-outbox.yml": 15,
    "miniflux-pause.yml": 20,
    "webhook-capacity.yml": 40,
    "private-release-config.yml": 25,
}


def _workflow(name):
    source = (WORKFLOWS / name).read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def test_all_existing_workflows_and_job_names_are_preserved():
    assert {path.name for path in WORKFLOWS.glob("*.yml")} == set(EXPECTED_JOBS)
    assert sum(len(jobs) for jobs in EXPECTED_JOBS.values()) == 9


@pytest.mark.parametrize("name", EXPECTED_JOBS)
def test_every_job_uses_a_literal_hosted_runner_and_can_be_dispatched(name):
    source, workflow = _workflow(name)
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


@pytest.mark.parametrize("name,minutes", EXPECTED_TIMEOUTS.items())
def test_native_job_timeouts_are_not_relaxed(name, minutes):
    _, workflow = _workflow(name)
    assert next(iter(workflow["jobs"].values()))["timeout-minutes"] == minutes


@pytest.mark.parametrize("name", ["miniflux-outbox.yml", "miniflux-pause.yml", "cutover-completion.yml"])
def test_postgres_remains_digest_locked_and_bounded(name):
    _, workflow = _workflow(name)
    job = next(iter(workflow["jobs"].values()))
    postgres = job["services"]["postgres"]
    assert postgres["image"] == (
        "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
    )
    assert "--memory 384m --memory-swap 384m --cpus 1 --pids-limit 64" in postgres["options"]
    assert postgres["env"] == {
        "POSTGRES_DB": "synthetic_outbox", "POSTGRES_PASSWORD": "synthetic-ci-only",
    }


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
    ("cutover-completion.yml", "scripts/verify_production_startup_native.py", True),
    ("cutover-completion.yml", "tests/test_joint_drain_controller.py", True),
    ("cutover-completion.yml", "tests/test_takeover_readonly_preflight.py", True),
    ("cutover-completion.yml", "docs/release-notes.md", False),
    ("cutover-completion.yml", "tests/test_numeric_facts.py", False),
    ("miniflux-pause.yml", "scripts/production_sender_driver.py", True),
    ("miniflux-pause.yml", "scripts/sender_drain_inventory.py", True),
    ("miniflux-pause.yml", "services/sender_drain_contract.py", True),
    ("miniflux-pause.yml", "tests/test_production_sender_driver.py", True),
    ("miniflux-pause.yml", "tests/test_sender_drain_contract.py", True),
    ("miniflux-pause.yml", "tests/test_sender_postgres_native.py", True),
    ("miniflux-pause.yml", "requirements-ops.txt", True),
    ("miniflux-pause.yml", "docs/release-notes.md", False),
    ("miniflux-pause.yml", "tests/test_numeric_facts.py", False),
])
def test_combined_cutover_regressions_trigger_the_relevant_native_gate(workflow, changed_path, expected):
    _, value = _workflow(workflow)
    triggers = value.get("on", value.get(True))
    for event in ["pull_request"] + (["push"] if workflow == "miniflux-pause.yml" else []):
        assert any(fnmatchcase(changed_path, pattern) for pattern in triggers[event]["paths"]) is expected


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
