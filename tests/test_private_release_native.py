"""Synthetic failure reporting and private seal tests without invoking Docker."""

import json
import io
from pathlib import Path
import subprocess

import pytest

from scripts import verify_private_release_native as native
from scripts import private_release_contract as private
from scripts import production_startup_fixture as fixture


def test_native_failure_retains_previous_stages_without_private_error_text(tmp_path):
    path = tmp_path / "report.json"
    report = {"status": "started", "checks": ["earlier-proof"]}
    native.advance_stage(path, report, "native_architecture")
    assert json.loads(path.read_text())["events"][0]["state"] == "started"
    native.advance_stage(path, report, "pull_previous_fixed_image")
    secret = "private-path-and-environment-value-must-not-leak"
    try:
        native.private.require(False, secret)
    except native.private.ContractError as error:
        native.fail_stage(path, report, error)

    saved = json.loads(path.read_text())
    assert saved["checks"] == ["earlier-proof"]
    assert saved["status"] == "failed"
    assert [(event["stage"], event["state"]) for event in saved["events"]] == [
        ("native_architecture", "started"), ("native_architecture", "completed"),
        ("pull_previous_fixed_image", "started"), ("pull_previous_fixed_image", "failed"),
    ]
    assert saved["failure"]["kind"] == "contract_refusal"
    assert saved["failure"]["message_sha256"] == native.private.sha(secret.encode())
    assert saved["failure"]["locations"] == [{
        "file": "private_release_contract.py", "function": "require",
        "line": native.private.require.__code__.co_firstlineno + 2,
    }]
    assert secret not in path.read_text()
    assert str(tmp_path) not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_unexpected_failure_has_no_exception_message_or_arbitrary_frame_path():
    try:
        raise OSError("/private/credentials: SECRET=synthetic-sensitive-value")
    except OSError as error:
        details = native.failure_details(error)
    assert details["kind"] == "unexpected_error"
    assert details["locations"] == []
    assert "SECRET" not in json.dumps(details)


def failed_build(tmp_path, monkeypatch, stdout=b"", stderr=b"", code=1, error=None):
    workspace = tmp_path / "private-build"
    workspace.mkdir(mode=0o700)
    report, calls = {}, []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if error:
            raise error
        return subprocess.CompletedProcess(args, code, stdout, stderr)
    monkeypatch.setattr(native.subprocess, "run", run)
    with pytest.raises(type(error) if error else native.private.ContractError) as caught:
        native.run_candidate_build(["docker", "build", "synthetic"], workspace, report)
    assert len(calls) == 1 and calls[0][1] == {"capture_output": True, "timeout": 1200, "check": False}
    return workspace, report["candidate_build_failure"], caught.value


def test_nonzero_build_keeps_private_originals_and_only_fixed_public_locations(tmp_path, monkeypatch):
    raw = (b"#11 [4/38] RUN pip install sensitive-inline-value\n"
           b"#11 0.5 ReadTimeoutError: https://token:private@private.invalid/simple\n"
           b"#11 1.5 [Errno 101] Network is unreachable\nDockerfile:36\n"
           b"runtime.yaml LLM_API_KEY=secret-value\nUNKNOWN-BARE-SECRET\n")
    workspace, report, error = failed_build(tmp_path, monkeypatch, stderr=raw, code=1)
    assert str(error) == "native candidate build failed"
    assert report["returncode"] == 1 and report["outcome"] == "nonzero_exit"
    assert report["elapsed_seconds"] >= 0 and report["container_build_completion"] == "unconfirmed"
    assert report["streams"]["stderr"]["events"] == [
        {"kind": "build_step", "step": 11, "position": 4, "total": 38, "instruction": "RUN"},
        {"kind": "dockerfile_location", "line": 36}]
    assert report["streams"]["stderr"]["marker_line_counts"] == {"network_timeout": 1, "network_unreachable": 1}
    public = json.dumps(report)
    for secret in ("sensitive-inline-value", "private.invalid", "runtime.yaml", "LLM_API_KEY", "secret-value", "UNKNOWN-BARE-SECRET"):
        assert secret not in public
    assert (workspace / "candidate-build.stderr").read_bytes() == raw
    assert (workspace / "candidate-build.stdout").read_bytes() == b""
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in workspace.iterdir())
    assert report["streams"]["stderr"]["complete_capture_sha256"] == private.sha(raw)
    assert report["retention"] == "private_bounded_copies_confirmed"
    assert not report["raw_private_workspace_uploaded"] and not report["execution_proof"]
    published = tmp_path / "report.json"
    native.fail_stage(published, {"candidate_build_failure": report}, error)
    assert json.loads(published.read_text())["status"] == "failed"
    assert "secret-value" not in published.read_text() and str(workspace) not in published.read_text()


@pytest.mark.parametrize("output", [None, b"partial output", "unexpected secret text"])
def test_build_timeout_preserves_original_exception_and_unknown_completion(tmp_path, monkeypatch, output):
    original = subprocess.TimeoutExpired(["docker", "build", "SECRET"], 1200, output=output, stderr=b"partial secret stderr")
    workspace, report, caught = failed_build(tmp_path, monkeypatch, error=original)
    assert caught is original and report["timed_out"] and report["returncode"] is None
    assert report["outcome"] == "timeout_unknown" and report["container_build_completion"] == "unconfirmed"
    assert report["streams"]["stdout"]["eof"] is None
    assert "SECRET" not in json.dumps(report) and "partial secret" not in json.dumps(report)
    assert (workspace / "candidate-build.stderr").read_bytes() == b"partial secret stderr"
    assert (workspace / "candidate-build.stdout").exists() == (type(output) is bytes)


def test_build_output_cap_is_explicit_and_total_preservation_stays_below_eight_mib(tmp_path, monkeypatch):
    raw = b"x" * (native.BUILD_STREAM_BYTES + 1)
    workspace, report, _ = failed_build(tmp_path, monkeypatch, stdout=raw, stderr=raw)
    for name in ("stdout", "stderr"):
        profile = report["streams"][name]
        assert profile["truncated"] and profile["observed_bytes"] == len(raw)
        assert profile["complete_capture_sha256"] is None and profile["retained_bytes"] == native.BUILD_STREAM_BYTES
        assert (workspace / ("candidate-build." + name)).read_bytes() == raw[:native.BUILD_STREAM_BYTES]
    assert sum(p.stat().st_size for p in workspace.iterdir()) + len(private.encoded(report)) <= 8 * 1024 * 1024
    assert "not_stream_bounded" in report["capture_behavior"]


@pytest.mark.parametrize("fault", ["write", "sync", "directory_sync", "replaced", "permissions",
    "existing", "symlink", "parent_mode", "projection"])
def test_build_preservation_failure_never_replaces_primary_or_retries(tmp_path, monkeypatch, fault):
    original = native.retain_build_outputs
    def broken(workspace, streams, profiles):
        if fault == "write":
            monkeypatch.setattr(native.os, "write", lambda *args: (_ for _ in ()).throw(OSError("secret disk error")))
        elif fault == "sync":
            monkeypatch.setattr(native.os, "fsync", lambda *args: (_ for _ in ()).throw(OSError("secret sync error")))
        elif fault == "directory_sync":
            sync = native.os.fsync
            calls = []
            def fail_directory(fd):
                calls.append(fd)
                if len(calls) == 3:
                    raise OSError("secret directory sync error")
                sync(fd)
            monkeypatch.setattr(native.os, "fsync", fail_directory)
        elif fault in {"replaced", "permissions"}:
            sync = native.os.fsync
            def mutate_after_sync(fd):
                sync(fd)
                if fault == "permissions":
                    native.os.fchmod(fd, 0o644)
                else:
                    path = workspace / "candidate-build.stdout"
                    path.rename(workspace / "candidate-build.stdout.before")
                    path.write_bytes(b"replacement")
            monkeypatch.setattr(native.os, "fsync", mutate_after_sync)
        elif fault == "existing":
            (workspace / "candidate-build.stdout").write_bytes(b"original evidence")
        elif fault == "symlink":
            (workspace / "candidate-build.stdout").symlink_to(tmp_path / "forbidden")
        elif fault == "parent_mode":
            workspace.chmod(0o755)
        return original(workspace, streams, profiles)
    monkeypatch.setattr(native, "retain_build_outputs", broken)
    if fault == "projection":
        monkeypatch.setattr(native, "build_failure_diagnostic", lambda *args: (_ for _ in ()).throw(ValueError("secret projection")))
    workspace, report, error = failed_build(tmp_path, monkeypatch, stdout=b"captured", stderr=b"second")
    assert str(error) == "native candidate build failed"
    assert report.get("retention", report.get("status")) == "evidence_insufficient"
    assert "secret" not in json.dumps(report) and not (tmp_path / "forbidden").exists()
    assert (workspace / "candidate-build.stderr").exists() == (fault == "directory_sync")
    if fault == "directory_sync":
        assert all(row["retention"] == "file_synced_directory_pending" for row in report["streams"].values())
    if fault == "existing":
        assert (workspace / "candidate-build.stdout").read_bytes() == b"original evidence"


def test_successful_build_adds_no_failure_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(native.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"secret", b""))
    report = {}
    native.run_candidate_build(["docker", "build", "synthetic"], tmp_path, report)
    assert not report and not list(tmp_path.iterdir())


@pytest.mark.parametrize("line", [b"Dockerfile:9 secret", b"#1 [4/38] EVIL secret", b"secret token",
    b"#1 [secret.domain 4/38] RUN secret", b"#1 [4/38] RUN secret\x00suffix", b"\xff\x00secret token\xfe"])
def test_unknown_build_lines_are_not_public_text(line):
    profile = native.build_output_profile(line)
    assert "secret" not in json.dumps(profile) and "EVIL" not in json.dumps(profile)


def test_candidate_build_uses_fixed_official_index_not_ambient_environment(tmp_path, monkeypatch):
    class Producer:
        stdout = io.BytesIO(b"synthetic archive")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def wait(self):
            return 0
    monkeypatch.setattr(native.subprocess, "Popen", lambda *args, **kwargs: Producer())
    monkeypatch.setattr(native.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0))
    monkeypatch.setenv("PIP_INDEX_URL", "https://secret.example.invalid/simple")
    monkeypatch.setenv("STARTUP_PIP_INDEX_URL", "https://other.example.invalid/simple")
    seen = []
    def build(args, workspace, report):
        seen.extend(args)
        raise private.ContractError("stop before inspect")
    monkeypatch.setattr(native, "run_candidate_build", build)
    source = {"source_sha": "a" * 40, "example_config_sha256": "b" * 64}
    contract = {"files": {"compose.json": "c" * 64, "runtime.yaml": "d" * 64}}
    with pytest.raises(private.ContractError, match="stop before inspect"):
        native.build_candidate(tmp_path, tmp_path, source, "synthetic:1", contract, "e" * 64, {})
    assert seen[:4] == ["docker", "build", "--platform", "linux/amd64"]
    assert [value for value in seen if value.startswith("PIP_INDEX_URL=")] == ["PIP_INDEX_URL=https://pypi.org/simple"]
    assert not any("secret.example" in value or "other.example" in value for value in seen)


def test_fixed_image_pull_failure_is_separate_from_start_and_preserved(tmp_path, monkeypatch):
    root = tmp_path / "public"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python@sha256:" + "a" * 64 + "\n")
    monkeypatch.setattr(native.private, "source_identity", lambda root: {"source_sha": "b" * 40})
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "info"]:
            return b"amd64\n"
        if args[:2] == ["docker", "pull"]:
            raise native.private.ContractError("private release subprocess failed")
        return b"fixture-strategy\n"

    monkeypatch.setattr(native.private, "run", fake_run)
    monkeypatch.setattr(native, "cleanup", lambda *args: None)
    output = tmp_path / "evidence"
    with pytest.raises(native.private.ContractError):
        native.execute(root, output)
    saved = json.loads((output / "report.json").read_text())
    assert saved["stage"] == "pull_previous_fixed_image"
    assert saved["events"][-1]["state"] == "failed"
    assert any(event["stage"] == "create_synthetic_resources" and event["state"] == "completed"
               for event in saved["events"])
    assert not any(args[:2] == ["docker", "compose"] for args in calls)
    assert saved["failure"]["locations"][0]["file"] == Path(native.__file__).name



def test_reader_identity_proof_keeps_capabilities_dropped_and_file_private(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_bytes(b"synthetic: true\n")
    config.chmod(0o400)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return b"" if len(calls) == 1 else native.private.sha(config.read_bytes()).encode() + b"\n"

    monkeypatch.setattr(native.private, "run", run)
    native.verify_config_reader_identity("python@sha256:" + "a" * 64, config, "1001:1001")
    assert [args[args.index("--user") + 1] for args in calls] == ["0:0", "1001:1001"]
    assert all(args[args.index("--cap-drop") + 1] == "ALL" and "--cap-add" not in args for args in calls)
    assert "except PermissionError" in calls[0][-1]
    assert config.stat().st_mode & 0o777 == 0o400


def test_native_manifest_seals_and_reloads_with_actual_private_contract(tmp_path, monkeypatch):
    """Only Docker discovery is doubled; Git/config/manifest/seal are real."""
    import subprocess
    from core import config
    repo = tmp_path / "public"
    repo.mkdir()
    (repo / "config.yaml").write_text("public_base_url: https://example.invalid\n")
    (repo / "docker-compose.yml").write_text("services: {}\n")
    (repo / "Dockerfile").write_text("FROM python@sha256:" + "b" * 64 + "\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Synthetic", "-c", "user.email=fixture@example.org", "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    paths = {name: directory / name for name in ("data", "fence", "grants", "slots", "project")}
    for path in paths.values():
        path.mkdir(mode=0o700)
    cfg, environment = directory / "runtime.yaml", directory / "environment"
    fixture.save(cfg, b"threshold:\n  pass: 7\nscoring_strategy_version: synthetic-startup\nfeeds: []\n")
    fixture.save(environment, b"AI_FILTER_API_KEY=synthetic-not-used-for-a-request\n")
    monkeypatch.setenv("AI_FILTER_API_KEY", "synthetic-not-used-for-a-request")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg))
    manifest = fixture.manifest_input(paths, "synthetic-native", "a" * 64, 4, {"synthetic": True})
    manifest_path = directory / "manifest.json"
    fixture.save(manifest_path, manifest)
    records = []
    for number, (name, service) in enumerate(manifest["services"].items()):
        mounts = [{"Type": "bind", "Source": str(cfg) if m["source"] == private.RUNTIME else m["source"],
                   "Destination": m["target"], "RW": not m.get("read_only", False)} for m in service["volumes"]]
        records.append({"Id": str(number) * 64, "Image": "sha256:" + "b" * 64, "State": {"Running": False},
            "Config": {"Labels": {"com.docker.compose.service": name}}, "Mounts": mounts,
            "HostConfig": {"PortBindings": {}}, "NetworkSettings": {"Networks": {"none": {}}}})
    original_run = private.run
    def run(args, **kwargs):
        if args[0] == "docker":
            assert "create" in args
            return b""
        return original_run(args, **kwargs)
    monkeypatch.setattr(private, "run", run)
    monkeypatch.setattr(private, "inspect_project", lambda _: records)
    p = {"directory": directory, "source": private.source_identity(repo), "manifest": manifest, "config": cfg,
         "paths": paths, "project": "synthetic-native", "manifest_path": manifest_path, "environment": environment,
         "strategy": config.load_config()["scoring_strategy_version"]}
    try:
        sealed = fixture.seal_inputs(repo, p)
        contract, actual, _digest = private.load(sealed, repo, require_fresh=True)
        assert contract["review"]["strategy"] == p["strategy"]
        assert set(actual["services"]) == {"ai-filter-api", "ai-filter-worker"}
        assert all(s["memswap_limit"] == "768m" and s["network_mode"] == "none" for s in actual["services"].values())
    finally:
        if (directory / "sealed").exists():
            (directory / "sealed").chmod(0o700)
