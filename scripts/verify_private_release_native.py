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
import shutil
import subprocess
import sys
import tempfile
import time


sys.path.insert(0, str(Path(__file__).resolve().parent))
import private_release_contract as private  # noqa: E402


def save(path, data):
    path.write_bytes(data if isinstance(data, bytes) else private.encoded(data))
    path.chmod(0o600)


def build_candidate(root, workspace, source, image, contract, contract_sha):
    context = workspace / "context"
    context.mkdir(mode=0o700)
    with subprocess.Popen(["git", "-C", str(root), "archive", "--format=tar", source["source_sha"]], stdout=subprocess.PIPE) as producer:
        subprocess.run(["tar", "-xf", "-", "-C", str(context)], stdin=producer.stdout, check=True)
        producer.stdout.close()
        private.require(producer.wait() == 0, "source archive failed")
    args = ["docker", "build", "--platform", "linux/amd64", "--tag", image]
    labels = {"AI_FILTER_BUILD_VERSION": image, "AI_FILTER_BUILD_GIT_SHA": source["source_sha"],
              "AI_FILTER_BUILD_SOURCE_ID": source["source_sha"] + ":clean",
              "AI_FILTER_BUILD_MANIFEST_SHA": contract["files"]["compose.json"],
              "AI_FILTER_BUILD_CONFIG_SHA": source["example_config_sha256"],
              "AI_FILTER_PRIVATE_RELEASE_SHA": contract_sha,
              "AI_FILTER_RUNTIME_CONFIG_SHA": contract["files"]["runtime.yaml"]}
    for key, value in labels.items():
        args += ["--build-arg", key + "=" + value]
    # Native image builds can exceed the bounded Docker inspection timeout.
    result = subprocess.run([*args, str(context)], capture_output=True, timeout=1200, check=False)
    private.require(result.returncode == 0, "native candidate build failed")
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
        require_arch = private.run(["docker", "info", "--format", "{{.Architecture}}"])
        private.require(require_arch.strip() in {b"x86_64", b"amd64"}, "native amd64 required")
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
                   "mem_limit": "384m", "pids_limit": 64,
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
        private.run(["docker", "compose", "--project-name", project, "--project-directory", str(project_dir),
                     "-f", str(previous_path), "up", "-d"])
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
        sealed = private.seal(root, config, manifest_path, environment, review_path, workspace / "sealed")
        contract, deployment, contract_sha = private.load(sealed, root, require_fresh=True)
        digest = build_candidate(root, workspace, source, image, contract, contract_sha)
        private.image_probe(contract, sealed.parent, digest)
        report["checks"].append("real_image_labels_embedded_example_and_private_loader_hash")
        private.preflight(contract, deployment)
        private.compose(contract, sealed.parent, digest, ["-f", str(sealed.parent / "compose.json"), "up", "-d", "--no-build", "--pull", "never"])
        private.verify_runtime(contract, deployment, sealed.parent, digest)
        report["checks"].append("actual_project_existing_volume_network_mount_hash_strategy")
        container = private.inspect_project(contract)[0]["Id"]
        write = subprocess.run(["docker", "exec", container, "python", "-c", "open('/app/config.yaml','ab').write(b'changed')"], capture_output=True, check=False)
        private.require(write.returncode != 0, "runtime config unexpectedly writable")
        report["checks"].append("runtime_bind_is_read_only")
        try:
            private.verify_runtime(contract, deployment, sealed.parent, "sha256:" + "0" * 64)
        except private.ContractError:
            report["checks"].append("wrong_image_refused")
        else:
            raise private.ContractError("wrong image accepted")
        save(config, config.read_bytes() + b"\n# mutation after gate\n")
        try:
            private.load(sealed, root, require_fresh=True)
        except private.ContractError:
            report["checks"].append("post_gate_input_mutation_refused")
        else:
            raise private.ContractError("mutated private input accepted")
        report.update(status="passed", image_id=digest, contract_sha256=contract_sha)
    except Exception:
        report["status"] = "failed"
        raise
    finally:
        save(report_path, report)
        # Only resources whose names were generated by this invocation are
        # removed. Synthetic private evidence stays on failed runners until
        # disposal; only the allowlisted public report is uploaded.
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"], capture_output=True, check=False).stdout.decode().split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=False)
        for args in (["network", "rm", network], ["volume", "rm", volume], ["image", "rm", image]):
            subprocess.run(["docker", *args], capture_output=True, check=False)
        if report["status"] == "passed":
            if sealed:
                sealed.parent.chmod(0o700)
            shutil.rmtree(workspace)


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
