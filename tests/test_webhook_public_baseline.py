"""Verify the published old baseline without private history or Docker access."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def driver():
    spec = importlib.util.spec_from_file_location(
        "public_baseline_driver", ROOT / "scripts/verify_webhook_capacity_native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(value).hexdigest()


def archive_bytes(entries):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w:xz", format=tarfile.USTAR_FORMAT) as archive:
        for name, value, kind, mode in entries:
            item = tarfile.TarInfo(name)
            item.mode = mode
            item.type = kind
            item.size = len(value) if kind == tarfile.REGTYPE else 0
            if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                item.linkname = "outside"
            archive.addfile(item, io.BytesIO(value) if item.isreg() else None)
    return target.getvalue()


def sample_manifest(name="module.py", value=b"synthetic\n", mode=0o644):
    return {"files": [{"path": name, "mode": mode, "bytes": len(value), "sha256": digest(value)}]}


def copied_package(driver, tmp_path):
    target = tmp_path / "repo"
    shutil.copytree(ROOT / driver.BASELINE_DIRECTORY, target / driver.BASELINE_DIRECTORY)
    return target


def test_published_baseline_extracts_offline_without_original_git_history(driver, tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("baseline extraction must not consult or fetch private Git history")

    monkeypatch.setattr(driver.subprocess, "check_output", forbidden)
    target = tmp_path / "context"
    result = driver.extract_baseline(ROOT, target)
    manifest, data = driver.baseline_inputs(ROOT)
    assert result["source_revision"] == "0f3f56120f95c2ddcec81f5d38b1287cfce2275c"
    assert result["source_kind"] == "sanitized_minimal_build_context"
    assert result["source_history_included"] is False
    assert result["archive_sha256"] == digest(data) == driver.BASELINE_ARCHIVE_SHA256
    assert result["manifest_sha256"] == driver.BASELINE_MANIFEST_SHA256
    assert len(manifest["files"]) == 129
    assert {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()} == {
        row["path"] for row in manifest["files"]}
    assert not (target / ".git").exists()
    assert not (target / "data").exists()
    assert not (target / "ops").exists()
    assert not (target / "tests").exists()
    for path in target.rglob("*"):
        if path.is_file():
            assert path.suffix not in {".db", ".sqlite", ".sqlite3"}
    config = (target / "config.yaml").read_text()
    assert "https://example.invalid" in config and "https://ai.example.invalid" in config
    assert "directory: /app/data/score_evidence/example-cohort" in config
    assert all("    - '" + code + "'" in config for code in ("990001", "990002", "990003"))


def test_all_docker_copy_inputs_are_present_and_only_build_inputs_are_packaged(driver):
    manifest, data = driver.baseline_inputs(ROOT)
    sources = {row["path"]: value for row, value in driver.baseline_members(manifest, data)}
    copy_inputs = [line.split()[1].rstrip("/") for line in sources["Dockerfile"].decode().splitlines()
                   if line.startswith("COPY ")]
    assert copy_inputs == manifest["copy_inputs"]
    for name in copy_inputs:
        assert name in sources or any(path.startswith(name + "/") for path in sources)
    assert all(name in {"Dockerfile", ".dockerignore", "docker-compose.yml"}
               or any(name == item or name.startswith(item + "/") for item in copy_inputs)
               for name in sources)
    # Capacity setup installs its synthetic configuration before importing the app.
    probe = (ROOT / "scripts/webhook_capacity_probe.py").read_text()
    assert probe.index('AI_FILTER_CONFIG_PATH=str(state / "config.yaml")') < probe.index(
        'importlib.import_module("routes.webhook_routes")')
    assert 'webhook.process_entry = forbidden' in probe


def test_baseline_receiver_build_and_dependency_bytes_remain_original(driver):
    manifest, data = driver.baseline_inputs(ROOT)
    sources = {row["path"]: value for row, value in driver.baseline_members(manifest, data)}
    locked = {
        "Dockerfile": "ce77e9153b6c16744f48a11d8f9bfab8716cc831da27270dcac385e482b8c851",
        "requirements-prod.txt": "12198fb8f3adda64991e88ed3d78df8ff37eb8fe4b782322c2469583b835cadc",
        "app.py": "e57331c8ccdc363a668d7bce0b342034de78792859dd02a9417fa0cd4058a9ea",
        "routes/webhook_routes.py": "65a2adcbd66b6737e717a159a329735fd58162de430aa5bb7b4a9caa4a6c374d",
        "services/webhook_receipts.py": "f58089f5b3a809e932f5c14533e90a8d6e37fee5372a12124418cb3bb4224680",
    }
    assert {name: digest(sources[name]) for name in locked} == locked


@pytest.mark.parametrize("name", ["baseline.tar.xz", "MANIFEST.json"])
def test_tampered_package_is_rejected_before_any_extraction(driver, tmp_path, name):
    repo = copied_package(driver, tmp_path)
    path = repo / driver.BASELINE_DIRECTORY / name
    path.write_bytes(path.read_bytes() + b"changed")
    destination = tmp_path / "context"
    with pytest.raises(ValueError, match="hash/size"):
        driver.extract_baseline(repo, destination)
    assert not destination.exists()


@pytest.mark.parametrize("name", ["baseline.tar.xz", "MANIFEST.json"])
def test_missing_package_input_cannot_fall_back_to_head_or_network(driver, tmp_path, name):
    repo = copied_package(driver, tmp_path)
    (repo / driver.BASELINE_DIRECTORY / name).unlink()
    with pytest.raises(FileNotFoundError):
        driver.extract_baseline(repo, tmp_path / "context")
    assert not (tmp_path / "context").exists()


@pytest.mark.parametrize("name", ["baseline.tar.xz", "MANIFEST.json"])
def test_package_files_cannot_be_symlinks(driver, tmp_path, name):
    repo = copied_package(driver, tmp_path)
    path = repo / driver.BASELINE_DIRECTORY / name
    outside = tmp_path / name
    path.replace(outside)
    path.symlink_to(outside)
    with pytest.raises(OSError):
        driver.extract_baseline(repo, tmp_path / "context")


def test_package_parent_symlink_rejected(driver, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").symlink_to(ROOT / "tests", target_is_directory=True)
    with pytest.raises(ValueError, match="directory must not be a symlink"):
        driver.extract_baseline(repo, tmp_path / "context")


def test_baseline_read_is_bounded_even_when_hash_would_match(driver, tmp_path):
    value = b"0123456789"
    path = tmp_path / "input"
    path.write_bytes(value)
    with pytest.raises(ValueError, match="bounded"):
        driver.baseline_file(path, maximum=9, expected_hash=digest(value))


def test_baseline_fifo_rejected_without_waiting_for_writer(tmp_path):
    path = tmp_path / "input"
    os.mkfifo(path)
    source = ROOT / "scripts/verify_webhook_capacity_native.py"
    script = (
        "import importlib.util,sys;"
        "s=importlib.util.spec_from_file_location('driver',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "m.baseline_file(sys.argv[2],maximum=100,expected_hash='0'*64)"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(source), str(path)],
                            capture_output=True, text=True, timeout=3, check=False)
    assert result.returncode != 0
    assert "baseline input is not a bounded ordinary file" in result.stderr


def test_baseline_nonblocking_read_keeps_ordinary_files_readable(driver, tmp_path):
    value = b"ordinary complete input\n"
    path = tmp_path / "input"
    path.write_bytes(value)
    assert driver.baseline_file(path, maximum=len(value), expected_hash=digest(value)) == value


@pytest.mark.parametrize("field,value", [
    ("format", 2), ("source_revision", "f" * 40), ("kind", "git_archive"),
    ("source_history_included", True), ("database_or_business_records_included", True),
])
def test_manifest_identity_is_required_independently_of_hash(driver, monkeypatch, field, value):
    manifest, data = driver.baseline_inputs(ROOT)
    manifest[field] = value

    def read(path, **_kwargs):
        return json.dumps(manifest).encode() if path.name == "MANIFEST.json" else data

    monkeypatch.setattr(driver, "baseline_file", read)
    with pytest.raises(ValueError, match="identity"):
        driver.baseline_inputs(ROOT)


@pytest.mark.parametrize("name", ["/absolute.py", "../outside.py", "a/../outside.py", "./file.py", "a//b.py"])
def test_path_escape_or_ambiguous_manifest_entries_rejected(driver, name):
    with pytest.raises(ValueError, match="unsafe"):
        driver.baseline_members(sample_manifest(name), b"")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE])
def test_special_archive_entries_cannot_be_materialized(driver, kind):
    data = archive_bytes([("module.py", b"synthetic\n", kind, 0o644)])
    with pytest.raises(ValueError, match="entry mismatch"):
        driver.baseline_members(sample_manifest(), data)


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "wrong-size", "wrong-content", "wrong-mode"])
def test_archive_must_match_manifest_exactly(driver, case):
    entry = ("module.py", b"synthetic\n", tarfile.REGTYPE, 0o644)
    entries = [entry]
    if case == "missing":
        entries = []
    elif case == "extra":
        entries += [("unlisted.py", b"x", tarfile.REGTYPE, 0o644)]
    elif case == "duplicate":
        entries += [entry]
    elif case == "wrong-size":
        entries = [("module.py", b"synthetic\nx", tarfile.REGTYPE, 0o644)]
    elif case == "wrong-content":
        entries = [("module.py", b"Synthetic\n", tarfile.REGTYPE, 0o644)]
    elif case == "wrong-mode":
        entries = [("module.py", b"synthetic\n", tarfile.REGTYPE, 0o755)]
    with pytest.raises(ValueError):
        driver.baseline_members(sample_manifest(), archive_bytes(entries))


def test_valid_regular_member_content_is_returned_exactly(driver):
    manifest = sample_manifest()
    data = archive_bytes([("module.py", b"synthetic\n", tarfile.REGTYPE, 0o644)])
    assert driver.baseline_members(manifest, data) == [(manifest["files"][0], b"synthetic\n")]


def test_expanded_source_budget_is_enforced_before_unpacking(driver):
    manifest = sample_manifest()
    manifest["files"][0]["bytes"] = driver.BASELINE_MAX_SOURCE_BYTES
    other = dict(manifest["files"][0], path="other.py", bytes=1)
    manifest["files"].append(other)
    with pytest.raises(ValueError, match="budget"):
        driver.baseline_members(manifest, b"")


def test_existing_evidence_destination_is_not_replaced(driver, tmp_path):
    target = tmp_path / "context"
    target.mkdir()
    old = target / "old-failure"
    old.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        driver.extract_baseline(ROOT, target)
    assert old.read_bytes() == b"preserve"


def test_baseline_build_records_sanitized_origin_without_old_git_archive(driver, monkeypatch, tmp_path):
    calls = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the old Git commit must not be extracted or fetched")

    source_identity = (driver.BASELINE + ":public-baseline:" + driver.BASELINE_ARCHIVE_SHA256
                       + ":derived-download-index-only:" + driver.BASELINE_DERIVED_DOCKERFILE_SHA256)

    def command(argv, *_args, **_kwargs):
        calls.append(argv)
        if "inspect" in argv:
            return json.dumps([{"Id": "sha256:synthetic", "Architecture": "amd64", "Created": "synthetic",
                                "Config": {"Labels": {"org.opencontainers.image.revision": driver.BASELINE,
                                                       "io.ai-filter.source.identity": source_identity}}}])
        return ""

    monkeypatch.setattr(driver, "extract_revision", forbidden)
    monkeypatch.setattr(driver, "command", command)
    result = driver.build_image(["docker"], ROOT, tmp_path, "baseline", driver.BASELINE, "test")
    assert result["source_kind"] == "sanitized_minimal_build_context"
    assert result["source_identity"] == source_identity
    assert "AI_FILTER_BUILD_SOURCE_ID=" + source_identity in calls[0]
    assert "--platform=linux/amd64" in calls[0]
    assert result["build_recipe"]["kind"] == "derived-download-index-only"
    assert result["build_recipe"]["url_replacements"] == 2
    assert result["build_recipe"]["original_context_unchanged"] is True
    assert calls[0][calls[0].index("--file") + 1] == str(tmp_path / "baseline/Dockerfile.download-index-only")
    saved = json.loads((tmp_path / "baseline/BUILD_PROVENANCE.json").read_text())
    assert saved["source_identity"] == result["source_identity"]
    assert saved["build_recipe"] == result["build_recipe"]


def test_unknown_baseline_revision_is_rejected_before_build(driver, tmp_path):
    with pytest.raises(ValueError, match="revision is fixed"):
        driver.build_image(["docker"], ROOT, tmp_path, "baseline", "f" * 40, "test")


@pytest.mark.parametrize("role", ["candidate", "baseline"])
def test_ci_build_index_is_fixed_for_candidate_without_rewriting_baseline(driver, monkeypatch, tmp_path, role):
    revision = "b" * 40 if role == "candidate" else driver.BASELINE
    source_identity = (revision + ":clean" if role == "candidate" else
                       revision + ":public-baseline:" + driver.BASELINE_ARCHIVE_SHA256
                       + ":derived-download-index-only:" + driver.BASELINE_DERIVED_DOCKERFILE_SHA256)
    calls = []
    monkeypatch.setenv("PIP_INDEX_URL", "https://untrusted.invalid/simple")
    monkeypatch.setenv("STARTUP_PIP_INDEX_URL", "https://untrusted.invalid/simple")

    def extract(_repo, _revision, context):
        return driver.extract_baseline(ROOT, context)["archive_sha256"]

    def command(argv, _folder, **kwargs):
        calls.append((argv, kwargs))
        if "inspect" in argv:
            return json.dumps([{"Id": "sha256:synthetic", "Architecture": "amd64", "Created": "synthetic",
                                "Config": {"Labels": {"org.opencontainers.image.revision": revision,
                                                       "io.ai-filter.source.identity": source_identity}}}])
        return ""

    monkeypatch.setattr(driver, "extract_revision", extract)
    monkeypatch.setattr(driver, "command", command)
    driver.build_image(["docker"], ROOT, tmp_path, role, revision, "test")
    argv, options = calls[0]
    index_values = [argv[i + 1] for i, value in enumerate(argv[:-1])
                    if value == "--build-arg" and argv[i + 1].startswith("PIP_INDEX_URL=")]
    assert index_values == (["PIP_INDEX_URL=https://pypi.org/simple"] if role == "candidate" else [])
    assert "untrusted.invalid" not in " ".join(argv)
    assert options == {"timeout": 900}
    assert len(calls) == 2  # One build, then its existing identity inspection; no retry.
    assert "--platform=linux/amd64" in argv and "--pull=false" in argv
    assert ("--file" in argv) is (role == "baseline")


def test_immutable_baseline_has_no_index_override_capability(driver):
    manifest, data = driver.baseline_inputs(ROOT)
    sources = {row["path"]: value for row, value in driver.baseline_members(manifest, data)}
    dockerfile = sources["Dockerfile"].decode()
    assert "ARG PIP_INDEX_URL" not in dockerfile
    assert dockerfile.count("-i https://pypi.tuna.tsinghua.edu.cn/simple") == 2
    assert digest(sources["Dockerfile"]) == "ce77e9153b6c16744f48a11d8f9bfab8716cc831da27270dcac385e482b8c851"


def test_baseline_download_derivation_preserves_all_original_context_and_archive_bytes(driver, tmp_path):
    folder = tmp_path / "case"
    folder.mkdir(mode=0o700)
    context = folder / "source"
    driver.extract_baseline(ROOT, context)
    original = {path.relative_to(context): path.read_bytes() for path in context.rglob("*") if path.is_file()}
    manifest, archive = driver.baseline_inputs(ROOT)
    path, recipe = driver.baseline_build_recipe(context, folder)
    assert path.parent == folder and path.parent != context
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == original[Path("Dockerfile")].replace(
        b"https://pypi.tuna.tsinghua.edu.cn/simple", b"https://pypi.org/simple")
    assert driver.sha(path) == recipe["derived_dockerfile_sha256"] == driver.BASELINE_DERIVED_DOCKERFILE_SHA256
    assert driver.sha(context / "Dockerfile") == recipe["original_dockerfile_sha256"] == driver.BASELINE_DOCKERFILE_SHA256
    assert {p.relative_to(context): p.read_bytes() for p in context.rglob("*") if p.is_file()} == original
    assert driver.baseline_inputs(ROOT) == (manifest, archive)
    assert digest(archive) == driver.BASELINE_ARCHIVE_SHA256
    with pytest.raises(FileExistsError):
        driver.baseline_build_recipe(context, folder)


@pytest.mark.parametrize("kind", ["unknown_url", "extra_url", "one_replacement", "other_bytes", "not_bytes"])
def test_baseline_derivation_rejects_any_nonfixed_source(driver, kind):
    manifest, archive = driver.baseline_inputs(ROOT)
    raw = next(value for row, value in driver.baseline_members(manifest, archive) if row["path"] == "Dockerfile")
    old = driver.BASELINE_PIP_INDEX.encode()
    if kind == "unknown_url":
        raw = raw.replace(old, b"https://untrusted.invalid/simple")
    elif kind == "extra_url":
        raw += b"\n# " + old
    elif kind == "one_replacement":
        raw = raw.replace(old, b"https://pypi.org/simple", 1)
    elif kind == "other_bytes":
        raw += b"\n"
    else:
        raw = raw.decode()
    with pytest.raises(ValueError, match="identity mismatch"):
        driver.derive_baseline_dockerfile(raw)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_baseline_derivation_independently_requires_exactly_two_url_replacements(driver, monkeypatch, count):
    raw = (b"-i " + driver.BASELINE_PIP_INDEX.encode() + b" \\\n") * count
    monkeypatch.setattr(driver, "BASELINE_DOCKERFILE_SHA256", digest(raw))
    with pytest.raises(ValueError, match="replacement count mismatch"):
        driver.derive_baseline_dockerfile(raw)


def test_baseline_derivation_rejects_changed_derived_digest(driver, monkeypatch):
    manifest, archive = driver.baseline_inputs(ROOT)
    raw = next(value for row, value in driver.baseline_members(manifest, archive) if row["path"] == "Dockerfile")
    monkeypatch.setattr(driver, "BASELINE_DERIVED_DOCKERFILE_SHA256", "f" * 64)
    with pytest.raises(ValueError, match="derived baseline Dockerfile identity mismatch"):
        driver.derive_baseline_dockerfile(raw)


def test_baseline_build_refuses_image_claiming_the_untouched_original_identity(driver, monkeypatch, tmp_path):
    original_identity = driver.BASELINE + ":public-baseline:" + driver.BASELINE_ARCHIVE_SHA256

    def command(argv, _folder, **_kwargs):
        if "inspect" in argv:
            return json.dumps([{"Id": "sha256:synthetic", "Architecture": "amd64", "Created": "synthetic",
                                "Config": {"Labels": {"org.opencontainers.image.revision": driver.BASELINE,
                                                       "io.ai-filter.source.identity": original_identity}}}])
        return ""

    monkeypatch.setattr(driver, "command", command)
    with pytest.raises(ValueError, match="platform/revision identity mismatch"):
        driver.build_image(["docker"], ROOT, tmp_path, "baseline", driver.BASELINE, "test")
    assert (tmp_path / "baseline/BUILD_PROVENANCE.json").is_file()
    assert not (tmp_path / "baseline/IMAGE.json").exists()
