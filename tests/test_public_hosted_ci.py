"""Public pull requests run synthetic gates on GitHub-hosted machines only."""

from pathlib import Path

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
}
EXPECTED_TIMEOUTS = {
    "admission-fence.yml": 8,
    "cutover-completion.yml": 15,
    "handoff-operations.yml": 8,
    "miniflux-outbox.yml": 15,
    "miniflux-pause.yml": 20,
    "webhook-capacity.yml": 40,
}


def _workflow(name):
    source = (WORKFLOWS / name).read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def test_all_existing_workflows_and_job_names_are_preserved():
    assert {path.name for path in WORKFLOWS.glob("*.yml")} == set(EXPECTED_JOBS)
    assert sum(len(jobs) for jobs in EXPECTED_JOBS.values()) == 8


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


@pytest.mark.parametrize("name", ["miniflux-outbox.yml", "miniflux-pause.yml"])
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
