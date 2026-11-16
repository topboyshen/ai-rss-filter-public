"""Native Docker proof of private config binding, using synthetic inputs only.

This intentionally does not manufacture a release-gate attestation or invoke
release.sh deploy. Main CI exercises the unchanged formal gate independently.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback


sys.path.insert(0, str(Path(__file__).resolve().parent))
import private_release_contract as private  # noqa: E402


BUILD_TIMEOUT = 1200
BUILD_EVIDENCE_BYTES = 8 * 1024 * 1024
BUILD_REPORT_BYTES = 64 * 1024
BUILD_STREAM_BYTES = (BUILD_EVIDENCE_BYTES - BUILD_REPORT_BYTES) // 2
BUILD_PIP_INDEX = "https://pypi.org/simple"


def save(path, data):
    path.write_bytes(data if isinstance(data, bytes) else private.encoded(data))
    path.chmod(0o600)
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def advance_stage(path, report, stage):
    if report.get("stage"):
        report["events"].append({"stage": report["stage"], "state": "completed", "at_ns": time.time_ns()})
    report["stage"] = stage
    report.setdefault("events", []).append({"stage": stage, "state": "started", "at_ns": time.time_ns()})
    save(path, report)


def failure_details(error):
    # Frame identifiers are from these committed scripts. Exception text may
    # contain YAML, environment, URLs or Docker output: persist only its hash.
    allowed = {str(Path(__file__).resolve()), str(Path(private.__file__).resolve())}
    locations = [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                 for frame in traceback.extract_tb(error.__traceback__) if str(Path(frame.filename).resolve()) in allowed]
    return {"kind": "contract_refusal" if isinstance(error, private.ContractError) else "unexpected_error",
            "message_sha256": private.sha(str(error).encode()), "locations": locations}


def fail_stage(path, report, error):
    report["status"] = "failed"
    report["failure"] = failure_details(error)
    report.setdefault("events", []).append({"stage": report.get("stage", "setup"), "state": "failed", "at_ns": time.time_ns()})
    save(path, report)


def cleanup(workspace, project, network, volume, image, sealed, report):
    # Remove only this invocation's synthetic resources. Never overwrite the
    # original failure record; the uploaded report contains no private inputs.
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"], capture_output=True, check=False).stdout.decode().split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=False)
    for args in (["network", "rm", network], ["volume", "rm", volume], ["image", "rm", image]):
        subprocess.run(["docker", *args], capture_output=True, check=False)
    if report["status"] == "passed":
        if sealed:
            sealed.parent.chmod(0o700)
        shutil.rmtree(workspace)


def verify_config_reader_identity(image, config_path, user):
    uid, _gid = private.numeric_user(user)
    other_user = "65534:65534" if uid == 0 else "0:0"
    base = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", "384m", "--pids-limit", "64",
            "--mount", f"type=bind,src={config_path},dst=/app/config.yaml,readonly", "--entrypoint", "python"]
    negative = "try:\n open('/app/config.yaml','rb').read()\nexcept PermissionError:\n pass\nelse:\n raise SystemExit(23)"
    private.run([*base, "--user", other_user, image, "-c", negative])
    actual = private.run([*base, "--user", user, image, "-c",
                          "import hashlib; print(hashlib.sha256(open('/app/config.yaml','rb').read()).hexdigest())"])
    private.require(actual.decode().strip() == private.sha(config_path.read_bytes()),
                    "reviewed UID cannot read its sealed config")


def build_output_profile(raw):
    """Only fixed classifications and numeric locations; never publish a line."""
    if type(raw) is not bytes:
        return {"status": "not_captured_unknown", "eof": None}
    retained = raw[:BUILD_STREAM_BYTES]
    lines = retained.splitlines()
    events, categories = [], {}
    markers = (("network_timeout", (b"ReadTimeoutError", b"Read timed out", b"read timed out", b"TLS handshake timeout")),
               ("network_unreachable", (b"[Errno 101] Network is unreachable", b"network is unreachable")),
               ("dns_resolution_failed", (b"Temporary failure in name resolution", b"Could not resolve host")),
               ("certificate_verification_failed", (b"CERTIFICATE_VERIFY_FAILED",)),
               ("pip_resolution_failed", (b"No matching distribution found", b"Could not find a version that satisfies")))
    for line in lines:
        step = re.fullmatch(rb"#([0-9]{1,6}) \[(?:[A-Za-z0-9_-]{1,64} )?([0-9]{1,4})/([0-9]{1,4})\] "
                            rb"(FROM|RUN|COPY|ADD|WORKDIR|ARG|ENV|USER|EXPOSE|CMD|ENTRYPOINT|LABEL)(?: .*)?", line)
        location = re.fullmatch(rb"Dockerfile:([0-9]{1,6})", line)
        if len(events) < 64 and step:
            events.append({"kind": "build_step", "step": int(step[1]), "position": int(step[2]),
                           "total": int(step[3]), "instruction": step[4].decode("ascii")})
        elif len(events) < 64 and location:
            events.append({"kind": "dockerfile_location", "line": int(location[1])})
        for category, needles in markers:
            if any(needle in line for needle in needles):
                categories[category] = categories.get(category, 0) + 1
    return {"status": "bounded_capture_projection", "observed_bytes": len(raw), "retained_bytes": len(retained),
            "retained_sha256": private.sha(retained), "complete_capture_sha256": private.sha(raw) if len(raw) <= BUILD_STREAM_BYTES else None,
            "truncated": len(raw) > BUILD_STREAM_BYTES, "eof": None, "line_count_in_retained_prefix": len(lines),
            "events": events, "events_capped": len(events) == 64, "marker_line_counts": categories,
            "marker_counts_are_not_root_cause_proof": True, "unclassified_text_not_published": True}


def retain_build_outputs(workspace, streams, profiles):
    """Bounded private copies only; the original subprocess capture is unchanged."""
    root_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root_fd)
        private.require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, "unsafe build evidence directory")
        for name in ("stdout", "stderr"):
            raw = streams[name]
            if type(raw) is not bytes:
                continue
            filename = "candidate-build." + name
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            try:
                view = memoryview(raw)[:BUILD_STREAM_BYTES]
                while view:
                    count = os.write(fd, view)
                    private.require(count > 0, "build evidence short write")
                    view = view[count:]
                os.fsync(fd)
                written = os.fstat(fd)
                linked = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                private.require(stat.S_ISREG(written.st_mode) and written.st_nlink == 1
                                and written.st_uid == os.geteuid() and not written.st_mode & 0o077
                                and (written.st_dev, written.st_ino) == (linked.st_dev, linked.st_ino)
                                and written.st_size == profiles[name]["retained_bytes"], "build evidence identity differs")
            finally:
                os.close(fd)
            profiles[name]["private_file"] = filename
            profiles[name]["retention"] = "file_synced_directory_pending"
        os.fsync(root_fd)
        current = os.stat(workspace, follow_symlinks=False)
        private.require((info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), "build evidence directory changed")
        for profile in profiles.values():
            if "private_file" in profile:
                profile["retention"] = "confirmed_private_only"
    finally:
        os.close(root_fd)


def build_failure_diagnostic(error, result, started, workspace):
    timed_out = isinstance(error, subprocess.TimeoutExpired)
    streams = {name: getattr(error if timed_out else result, name, None) for name in ("stdout", "stderr")}
    profiles = {name: build_output_profile(raw) for name, raw in streams.items()}
    code = getattr(result, "returncode", None)
    diagnostic = {"schema": 1, "operation": "docker_build", "stage": "build_and_check_candidate_image", "execution_proof": False,
        "outcome": "timeout_unknown" if timed_out else "nonzero_exit" if type(code) is int else "execution_error_unknown",
        "returncode": code if type(code) is int else None, "elapsed_seconds": round(time.monotonic() - started, 6),
        "timeout_seconds": BUILD_TIMEOUT, "timed_out": timed_out,
        "container_build_completion": "unconfirmed", "streams": profiles,
        "capture_behavior": "original_subprocess_run_capture_output_not_stream_bounded",
        "preservation_budget_bytes": BUILD_EVIDENCE_BYTES, "raw_private_workspace_uploaded": False}
    try:
        retain_build_outputs(workspace, streams, profiles)
        diagnostic["retention"] = ("private_bounded_copies_confirmed" if any(type(raw) is bytes for raw in streams.values())
                                   else "not_captured_unknown")
    except Exception as preservation_error:
        diagnostic["retention"] = "evidence_insufficient"
        diagnostic["preservation_error"] = {"kind": "os_error" if isinstance(preservation_error, OSError)
            else "contract_refusal" if isinstance(preservation_error, private.ContractError) else "unexpected_error",
            "message_sha256": private.sha(str(preservation_error).encode())}
    private.require(len(private.encoded(diagnostic)) <= BUILD_REPORT_BYTES, "build failure projection capacity")
    return diagnostic


def run_candidate_build(args, workspace, report):
    started, result = time.monotonic(), None
    try:
        result = subprocess.run(args, capture_output=True, timeout=BUILD_TIMEOUT, check=False)
        private.require(result.returncode == 0, "native candidate build failed")
    except Exception as error:
        try:
            report["candidate_build_failure"] = build_failure_diagnostic(error, result, started, workspace)
        except Exception as diagnostic_error:
            report["candidate_build_failure"] = {"schema": 1, "operation": "docker_build", "execution_proof": False,
                "status": "evidence_insufficient", "primary_failure_preserved": True,
                "diagnostic_error_sha256": private.sha(str(diagnostic_error).encode())}
        raise


def build_candidate(root, workspace, source, image, contract, contract_sha, report):
    context = workspace / "context"
    context.mkdir(mode=0o700)
    with subprocess.Popen(["git", "-C", str(root), "archive", "--format=tar", source["source_sha"]], stdout=subprocess.PIPE) as producer:
        subprocess.run(["tar", "-xf", "-", "-C", str(context)], stdin=producer.stdout, check=True)
        producer.stdout.close()
        private.require(producer.wait() == 0, "source archive failed")
    args = ["docker", "build", "--platform", "linux/amd64", "--tag", image,
            "--build-arg", "PIP_INDEX_URL=" + BUILD_PIP_INDEX]
    labels = {"AI_FILTER_BUILD_VERSION": image, "AI_FILTER_BUILD_GIT_SHA": source["source_sha"],
              "AI_FILTER_BUILD_SOURCE_ID": source["source_sha"] + ":clean",
              "AI_FILTER_BUILD_MANIFEST_SHA": contract["files"]["compose.json"],
              "AI_FILTER_BUILD_CONFIG_SHA": source["example_config_sha256"],
              "AI_FILTER_PRIVATE_RELEASE_SHA": contract_sha,
              "AI_FILTER_RUNTIME_CONFIG_SHA": contract["files"]["runtime.yaml"]}
    for key, value in labels.items():
        args += ["--build-arg", key + "=" + value]
    # Native image builds can exceed the bounded Docker inspection timeout.
    run_candidate_build([*args, str(context)], workspace, report)
    image_info = json.loads(private.run(["docker", "image", "inspect", image]))[0]
    digest = image_info["Id"]
    for key, value in {"io.ai-filter.private-release.sha256": contract_sha,
                       "io.ai-filter.runtime-config.sha256": contract["files"]["runtime.yaml"],
                       "io.ai-filter.config.sha256": source["example_config_sha256"],
                       "org.opencontainers.image.revision": source["source_sha"]}.items():
        private.require(image_info["Config"]["Labels"].get(key) == value, "native image label mismatch")
    embedded = private.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", digest,
                            "-c", "import hashlib; print(hashlib.sha256(open('/app/config.yaml','rb').read()).hexdigest())"])
    private.require(embedded.decode().strip() == source["example_config_sha256"], "embedded example config mismatch")
    return digest


def execute(root, output):
    source = private.source_identity(root)
    report = {"schema": "private-release-native-v1", "source_sha": source["source_sha"],
              "synthetic_only": True, "production_deploy": False, "status": "started", "checks": []}
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report_path = output / "report.json"
    save(report_path, report)
    workspace = Path(tempfile.mkdtemp(prefix="ai-filter-private-native-")).resolve()
    project = "private-native-" + str(os.getpid())
    image = "ai-filter-private-native:1-" + str(os.getpid())
    network = project + "-network"
    volume = project + "-volume"
    sealed = None
    try:
        advance_stage(report_path, report, "native_architecture")
        require_arch = private.run(["docker", "info", "--format", "{{.Architecture}}"])
        private.require(require_arch.strip() in {b"x86_64", b"amd64"}, "native amd64 required")
        advance_stage(report_path, report, "prepare_synthetic_inputs")
        project_dir = workspace / "project"
        project_dir.mkdir(mode=0o700)
        data = project_dir / "data"
        data.mkdir(mode=0o700)
        config = workspace / "runtime.yaml"
        save(config, b"threshold:\n  pass: 7\nscoring_strategy_version: synthetic-native\n")
        environment = workspace / "environment"
        save(environment, b"LLM_API_KEY=synthetic-never-sent-to-a-model\n")
        strategy = private.run([sys.executable, "-c",
                                "from core import config; config.CONFIG_PATH=" + repr(str(config))
                                + "; print(config.load_config()['scoring_strategy_version'])"],
                               env={"PATH": os.environ["PATH"], "PYTHONPATH": str(root)}).decode().strip()
        service = {"image": private.IMAGE, "command": ["python", "-c", "import time; time.sleep(600)"],
                   "mem_limit": "384m", "pids_limit": 64, "user": f"{os.geteuid()}:{os.getegid()}",
                   "environment": {"AI_FILTER_CONFIG_PATH": "/app/config.yaml", "LLM_API_KEY": "${LLM_API_KEY:?}"},
                   "volumes": [{"type": "bind", "source": str(data), "target": "/app/data", "bind": {"create_host_path": False}},
                               {"type": "bind", "source": private.RUNTIME, "target": "/app/config.yaml", "read_only": True},
                               {"type": "volume", "source": "persistent", "target": "/app/data/synthetic-persistent"}],
                   "networks": ["existing"], "healthcheck": {"disable": True}}
        manifest = {"name": project, "services": {"ai-filter-api": service},
                    "networks": {"existing": {"external": True, "name": network}},
                    "volumes": {"persistent": {"external": True, "name": volume}}}
        manifest_path = workspace / "manifest.json"
        save(manifest_path, manifest)
        # Establish a genuine existing synthetic project before the review.
        # Its old Python container only sleeps; no application/model is started.
        advance_stage(report_path, report, "create_synthetic_resources")
        private.run(["docker", "network", "create", "--internal", network])
        private.run(["docker", "volume", "create", volume])
        previous = deepcopy(manifest)
        previous_service = previous["services"]["ai-filter-api"]
        base_image = next(line.split()[1] for line in (root / "Dockerfile").read_text().splitlines() if line.startswith("FROM "))
        previous_service["image"] = base_image
        previous_service["volumes"][1]["source"] = str(config)
        previous_service["environment"]["LLM_API_KEY"] = "synthetic-never-sent-to-a-model"
        previous_path = workspace / "previous.json"
        save(previous_path, previous)
        advance_stage(report_path, report, "pull_previous_fixed_image")
        private.run(["docker", "pull", base_image])
        advance_stage(report_path, report, "start_previous_synthetic_project")
        private.run(["docker", "compose", "--project-name", project, "--project-directory", str(project_dir),
                     "-f", str(previous_path), "up", "-d", "--no-build", "--pull", "never"])
        advance_stage(report_path, report, "inspect_previous_synthetic_project")
        existing = private.runtime_topology(private.inspect_project({"review": {"project": project, "project_directory": str(project_dir)}}))
        review = {**source, "approved": True, "approved_by": "synthetic-native-workflow",
                  "approved_at": int(time.time()) - 1, "expires_at": int(time.time()) + 3600,
                  "runtime_config_sha256": private.sha(config.read_bytes()),
                  "input_manifest_sha256": private.sha(manifest_path.read_bytes()),
                  "environment_sha256": private.sha(environment.read_bytes()),
                  "strategy": strategy, "project": project, "project_directory": str(project_dir),
                  "health_services": ["ai-filter-api"], "existing_topology": existing,
                  "service_mapping": {"ai-filter-api": ["ai-filter-api"]},
                  "topology": {"ai-filter-api": {"mounts": service["volumes"], "networks": ["existing"], "ports": []}}}
        review_path = workspace / "review.json"
        save(review_path, review)
        advance_stage(report_path, report, "seal_private_inputs")
        sealed = private.seal(root, config, manifest_path, environment, review_path, workspace / "sealed")
        advance_stage(report_path, report, "check_private_seal")
        contract, deployment, contract_sha = private.load(sealed, root, require_fresh=True)
        advance_stage(report_path, report, "prove_config_reader_identity")
        verify_config_reader_identity(base_image, sealed.parent / "runtime.yaml", service["user"])
        report["checks"].append("mismatched_uid_denied_reviewed_uid_reads_private_config")
        advance_stage(report_path, report, "build_and_check_candidate_image")
        digest = build_candidate(root, workspace, source, image, contract, contract_sha, report)
        advance_stage(report_path, report, "probe_private_runtime_config")
        private.image_probe(contract, sealed.parent, digest)
        report["checks"].append("real_image_labels_embedded_example_and_private_loader_hash")
        advance_stage(report_path, report, "preflight_existing_project")
        private.preflight(contract, deployment)
        advance_stage(report_path, report, "switch_synthetic_project")
        private.compose(contract, sealed.parent, digest, ["-f", str(sealed.parent / "compose.json"), "up", "-d", "--no-build", "--pull", "never"])
        advance_stage(report_path, report, "verify_synthetic_runtime")
        private.verify_runtime(contract, deployment, sealed.parent, digest)
        report["checks"].append("actual_project_existing_volume_network_mount_hash_strategy")
        advance_stage(report_path, report, "prove_read_only_mount")
        container = private.inspect_project(contract)[0]["Id"]
        write = subprocess.run(["docker", "exec", container, "python", "-c", "open('/app/config.yaml','ab').write(b'changed')"], capture_output=True, check=False)
        private.require(write.returncode != 0, "runtime config unexpectedly writable")
        report["checks"].append("runtime_bind_is_read_only")
        advance_stage(report_path, report, "reject_wrong_image")
        try:
            private.verify_runtime(contract, deployment, sealed.parent, "sha256:" + "0" * 64)
        except private.ContractError:
            report["checks"].append("wrong_image_refused")
        else:
            raise private.ContractError("wrong image accepted")
        advance_stage(report_path, report, "reject_changed_input")
        save(config, config.read_bytes() + b"\n# mutation after gate\n")
        try:
            private.load(sealed, root, require_fresh=True)
        except private.ContractError:
            report["checks"].append("post_gate_input_mutation_refused")
        else:
            raise private.ContractError("mutated private input accepted")
        report["events"].append({"stage": report["stage"], "state": "completed", "at_ns": time.time_ns()})
        report.update(status="passed", image_id=digest, contract_sha256=contract_sha)
    except Exception as error:
        fail_stage(report_path, report, error)
        raise
    finally:
        save(report_path, report)
        cleanup(workspace, project, network, volume, image, sealed, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        execute(args.repo.resolve(), args.output.resolve())
    except Exception:
        print("Native private release verification failed; see public report status", file=sys.stderr)
        raise SystemExit(1) from None
    print("Native private release verification passed (synthetic only)")


if __name__ == "__main__":
    main()
