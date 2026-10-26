import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.release_status import (
    _default_registry, _fmt, _registry_diag, _resolve_registry, collect,
    compare_versions, main, read_attestation, read_history, read_lock, read_release_state,
)


def _registry(tmp_path, current=("my-ai-filter:1", "sha256:" + "a" * 64),
              previous=("my-ai-filter:0", "sha256:" + "b" * 64)):
    reg = tmp_path / "releases"
    reg.mkdir()
    (reg / "current.state").write_text("\n".join(current) + "\n")
    if previous:
        (reg / "previous.state").write_text("\n".join(previous) + "\n")
    return reg


def test_empty_and_missing_state_are_unknown(tmp_path):
    reg = tmp_path / "releases"
    reg.mkdir()
    (reg / "current.state").write_text("")
    state = read_release_state(reg)
    assert state["current"]["status"] == "unknown"
    assert state["previous"]["status"] == "unknown"


def test_attestation_requires_magic_and_commit_binding(tmp_path):
    att = tmp_path
    commit = "c" * 40
    assert read_attestation(att, None)["status"] == "not_requested"
    assert read_attestation(att, commit)["status"] == "no_evidence"
    (att / f"{commit}.state").write_text("garbage\n")
    assert read_attestation(att, commit)["status"] == "unknown"
    (att / f"{commit}.state").write_text("ai-filter-release-test-attestation-v1\n" + "d" * 40 + ":clean\npassed\n")
    assert read_attestation(att, commit)["status"] == "unknown"
    (att / f"{commit}.state").write_text("ai-filter-release-test-attestation-v1\n" + commit + ":clean\npassed\n")
    result = read_attestation(att, commit)
    assert result["status"] == "passed"
    (att / f"{commit}.state").write_text("ai-filter-release-test-attestation-v1\n" + commit + ":clean\nfailed\n")
    assert read_attestation(att, commit)["status"] == "failed"


def test_history_missing_rows_is_not_running(tmp_path):
    reg = _registry(tmp_path)
    (reg / "history.tsv").write_text("2026-01-01T00:00:00Z\tbuild\tmy-ai-filter:9\tsha256:x\tbuilt\tm\ts\n")
    history = read_history(reg, "my-ai-filter:1")
    assert history["stages"] == {}
    assert history["completed"] is False
    text = _fmt(collect(reg, tmp_path, "my-ai-filter:1", None))
    assert "尚无完成记录" in text
    assert "进行中" not in text


def test_history_reports_stages_for_the_candidate_only(tmp_path):
    reg = _registry(tmp_path)
    (reg / "history.tsv").write_text(
        "2026-01-01T00:00:00Z\tbuild\tmy-ai-filter:9\tsha256:x\tbuilt\tm\ts\n"
        "2026-01-02T00:00:00Z\tbuild\tmy-ai-filter:1\tsha256:a\tbuilt\tm\ts\n"
        "2026-01-02T00:01:00Z\tdeploy\tmy-ai-filter:1\tsha256:a\thealthy\tm\ts\n"
    )
    history = read_history(reg, "my-ai-filter:1")
    assert history["completed"] is True
    assert set(history["stages"]) == {"build", "deploy"}


def test_lock_free_busy_and_unknown(tmp_path):
    reg = _registry(tmp_path)
    assert read_lock(reg)["status"] == "free"
    lock = reg / "release.lock"
    lock.write_text("")
    assert read_lock(reg)["status"] == "free"
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = read_lock(reg)
        assert busy["status"] == "busy"
        assert busy["holder"] == "unknown"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    directory = tmp_path / "as-dir.lock"
    directory.mkdir()
    assert read_lock(tmp_path)["status"] in {"free", "unknown"}


def test_version_match_same_different_and_unknown(tmp_path):
    known = {"status": "known", "digest": "sha256:a"}
    assert compare_versions(known, {"digest": "sha256:a"}) == "same"
    assert compare_versions(known, {"digest": "sha256:b"}) == "different"
    assert compare_versions(known, None) == "unknown"
    assert compare_versions({"status": "unknown"}, {"digest": "sha256:a"}) == "unknown"


def test_collect_without_candidate_does_not_guess(tmp_path):
    reg = _registry(tmp_path)
    (reg / "history.tsv").write_text("2026-01-01T00:00:00Z\tbuild\tmy-ai-filter:1\tsha256:a\tbuilt\tm\ts\n")
    data = collect(reg, tmp_path, None, None)
    assert data["candidate_commit"] is None
    assert data["gate"]["status"] == "not_requested"
    assert data["version_match"] == "unknown"

# ----------------------------------------------------------------------
# CWD-independence / resolution diagnostics (fix 2026-10-16)
# ----------------------------------------------------------------------

def test_default_registry_is_script_anchored():
    repo_root = Path(__file__).resolve().parents[1]
    assert _default_registry() == repo_root / "data" / "releases"


def test_resolve_registry_default_anchor():
    """Explicit None resolves to script-anchored (matches default)."""
    assert _resolve_registry(None) == _default_registry()


def test_resolve_registry_explicit_absolute_kept(tmp_path):
    raw = str(tmp_path / "data" / "releases")
    assert _resolve_registry(raw) == Path(raw)


def test_resolve_registry_relative_resolves_to_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    got = _resolve_registry("data/releases")
    assert got.is_absolute()
    assert got == (tmp_path / "data" / "releases").resolve()


def test_registry_diag_reports_missing(tmp_path):
    diag = _registry_diag(tmp_path / "does-not-exist")
    assert diag["status"] == "missing"
    assert "does not exist" in diag["reason"]


def test_registry_diag_reports_missing_state_files(tmp_path):
    reg = tmp_path / "releases"
    reg.mkdir()
    (reg / "previous.state").write_text("my-ai-filter:1\nsha256:digest\n")
    diag = _registry_diag(reg)
    assert diag["status"] == "known"  # directory itself exists
    assert diag["current.state.missing"] is True
    assert "previous.state.missing" not in diag


def test_registry_diag_reports_incomplete_state(tmp_path):
    reg = tmp_path / "releases"
    reg.mkdir()
    (reg / "current.state").write_text("only-one-line\n")
    (reg / "previous.state").write_text("tag\nsha256:digest\n")
    diag = _registry_diag(reg)
    assert diag["current.state.incomplete"].startswith("1 ")


@pytest.fixture
def isolated_cli(tmp_path):
    """Copy the real script into a synthetic source tree, with no host state."""
    source_root = tmp_path / "source"
    scripts = source_root / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "release_status.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts" / script.name, script)
    data = source_root / "data"
    data.mkdir()
    return script, _registry(data)


def _registry_snapshot(registry):
    return {
        path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in registry.iterdir()
    }


def _run_status_cli(script, cwd, *args):
    result = subprocess.run(
        [sys.executable, "-B", str(script), *args],
        cwd=cwd, capture_output=True, text=True, timeout=5, check=True,
    )
    assert result.stderr == ""
    return result.stdout


@pytest.mark.parametrize("cwd_name", ["repo", "tmp", "etc"])
def test_status_from_arbitrary_cwd_reports_known(isolated_cli, tmp_path, cwd_name):
    """The real CLI reads its synthetic source tree, even beside a decoy registry."""
    script, registry = isolated_cli
    source_root = script.parents[1]
    cwd = source_root if cwd_name == "repo" else tmp_path / cwd_name
    cwd.mkdir(exist_ok=True)
    if cwd != source_root:
        (cwd / "data").mkdir()
        _registry(cwd / "data", current=("wrong-cwd", "sha256:" + "c" * 64))
    before = _registry_snapshot(registry)
    out = _run_status_cli(script, cwd)
    assert "当前生产: my-ai-filter:1 sha256:aaaaaaaaaaaa (known)" in out
    assert "上一版本: my-ai-filter:0 (known)" in out
    assert f"registry: {registry}" in out
    assert "wrong-cwd" not in out
    assert _registry_snapshot(registry) == before


def test_default_cli_missing_current_is_unknown_even_when_previous_is_known(isolated_cli, tmp_path):
    script, registry = isolated_cli
    (registry / "current.state").unlink()
    before = _registry_snapshot(registry)
    out = _run_status_cli(script, tmp_path)
    assert "当前生产: 未知  (unknown)" in out
    assert "上一版本: my-ai-filter:0 (known)" in out
    assert "registry-detail: current.state.missing: True" in out
    assert _registry_snapshot(registry) == before


@pytest.mark.parametrize("content", ["", "only-one-line\n"])
def test_default_cli_incomplete_current_stays_unknown(isolated_cli, tmp_path, content):
    script, registry = isolated_cli
    (registry / "current.state").write_text(content)
    before = _registry_snapshot(registry)
    out = _run_status_cli(script, tmp_path)
    assert "当前生产: 未知  (unknown)" in out
    assert "registry-detail: current.state.incomplete:" in out
    assert _registry_snapshot(registry) == before


def test_explicit_absolute_cli_registry_overrides_default(isolated_cli, tmp_path):
    script, default_registry = isolated_cli
    explicit = _registry(tmp_path, current=("explicit", "sha256:" + "d" * 64))
    before = (_registry_snapshot(default_registry), _registry_snapshot(explicit))
    out = _run_status_cli(script, script.parents[1], "--registry", str(explicit))
    assert "当前生产: explicit sha256:dddddddddddd (known)" in out
    assert f"registry: {explicit}" in out
    assert (_registry_snapshot(default_registry), _registry_snapshot(explicit)) == before


def test_unreadable_current_is_reported_without_using_previous(tmp_path, monkeypatch, capsys):
    registry = _registry(tmp_path)
    original_read = Path.read_text

    def deny_current(path, *args, **kwargs):
        if path == registry / "current.state":
            raise PermissionError("synthetic permission denied")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_current)
    assert main(["--registry", str(registry)]) == 0
    out = capsys.readouterr().out
    assert "当前生产: 未知  (unknown)" in out
    assert "上一版本: my-ai-filter:0 (known)" in out
    assert "registry-detail: current.state.unreadable: synthetic permission denied" in out


def test_status_explicit_relative_from_cwd(tmp_path, monkeypatch, capsys):
    """Explicit relative registry preserves cwd-relative lookup semantics."""
    reg = tmp_path / "data" / "releases"
    (tmp_path / "data").mkdir()
    reg.mkdir()
    (reg / "current.state").write_text("synthetic\nsha256:digest\n")
    (reg / "previous.state").write_text("synthetic-prev\nsha256:digest\n")
    monkeypatch.chdir(tmp_path)
    assert main(["--registry", "data/releases"]) == 0
    out = capsys.readouterr().out
    resolved = (tmp_path / "data" / "releases").resolve()
    assert f"registry: {resolved}" in out
    assert "(known)" in out
