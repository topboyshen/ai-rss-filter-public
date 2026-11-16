"""The diagnostics helper must accept the state the release script reports."""

import argparse
import hashlib

import pytest

from scripts import capture_release_diagnostics as diagnostics


def _args(tmp_path, failed_state):
    registry = tmp_path / "registry"
    project = tmp_path / "project"
    registry.mkdir(mode=0o700)
    project.mkdir(mode=0o700)
    compose = project / "compose.yml"
    compose.write_text("services:\n  redis: {image: redis:7}\n", encoding="utf-8")
    return argparse.Namespace(
        registry_dir=str(registry),
        project_dir=str(project),
        compose_file=str(compose),
        image_ref="sha256:" + "a" * 64,
        manifest_hash=hashlib.sha256(compose.read_bytes()).hexdigest(),
        phase="health",
        failed_service="redis",
        failed_state=failed_state,
        services=["redis"],
    )


@pytest.mark.parametrize(
    "value",
    [
        "running|unhealthy",
        "exited|healthy",
        "created|starting",
        "running|none",
        "missing",
        "unknown",
    ],
)
def test_states_the_release_script_reports_are_allowed(value):
    assert diagnostics.failed_state_is_allowed(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bogus",
        "running",
        "healthy|running",
        "running|bogus",
        "missing|healthy",
        "ambiguous|2",
        "running|unhealthy|extra",
        "running|",
        "|healthy",
    ],
)
def test_states_outside_the_release_contract_are_rejected(value):
    assert not diagnostics.failed_state_is_allowed(value)


def test_validate_args_accepts_a_composite_failed_state(tmp_path):
    validated = diagnostics.validate_args(_args(tmp_path, "running|unhealthy"))

    assert validated[-1] == ["redis"]


@pytest.mark.parametrize("value", ["bogus", "running|bogus", "missing|healthy"])
def test_validate_args_still_rejects_unknown_states(tmp_path, value):
    with pytest.raises(diagnostics.SafetyError):
        diagnostics.validate_args(_args(tmp_path, value))
