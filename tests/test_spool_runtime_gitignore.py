"""Git's real ignore and release guard must distinguish runtime files from code."""

import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git(repo, env, *arguments):
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], env=env,
        text=True, capture_output=True, check=True, timeout=10,
    ).stdout.strip()


def _commit(repo, env):
    _git(
        repo, env, "-c", "user.name=Gitignore Test",
        "-c", "user.email=gitignore-test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture",
    )


@pytest.fixture
def clean_repo(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    _git(repo, env, "init", "--quiet", "--template=")
    _git(repo, env, "config", "core.excludesFile", os.devnull)
    _git(repo, env, "config", "core.hooksPath", os.devnull)
    (repo / ".gitignore").write_bytes((PROJECT_ROOT / ".gitignore").read_bytes())
    (repo / "tracked.py").write_text("original = True\n", encoding="utf-8")
    _git(repo, env, "add", ".gitignore", "tracked.py")
    _commit(repo, env)
    assert not _status(repo, env)
    return repo, env


def _status(repo, env):
    return _git(repo, env, "status", "--porcelain=v1", "--untracked-files=all")


def _release_guard(repo, env):
    # Execute the source function unchanged, not the release entrypoint: sourcing
    # the latter would create a registry, take locks and dispatch commands.
    source = (PROJECT_ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
    start = source.index("require_clean_release_tree() {\n")
    end = source.index("\n}\n", start) + len("\n}\n")
    script = (
        'set -eu\nROOT="$1"\n' + source[start:end]
        + '\nrequire_clean_release_tree build my-ai-filter:gitignore-test\n'
        + 'printf "%s\\n" "$RELEASE_SOURCE_ID"\n'
    )
    return subprocess.run(
        ["bash", "-s", "--", str(repo)], input=script, env=env,
        text=True, capture_output=True, check=False, timeout=10,
    )


def _assert_release_clean(repo, env):
    assert _status(repo, env) == ""
    result = _release_guard(repo, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{_git(repo, env, 'rev-parse', 'HEAD')}:clean"


def _assert_release_dirty(repo, env, relative):
    assert relative in _status(repo, env)
    result = _release_guard(repo, env)
    assert result.returncode == 7
    assert "Refusing build from a dirty Git worktree" in result.stderr
    assert ":clean" not in result.stdout


@pytest.mark.parametrize("target_name", ["a" * 64 + ".json", ".completed-digests"])
def test_actual_atomic_spool_write_keeps_git_and_release_guard_clean(
    clean_repo, monkeypatch, target_name,
):
    from routes import webhook_routes

    repo, env = clean_repo
    spool = repo / "data" / "webhook_spool"
    target = spool / target_name
    observed = []

    def inspect_before_replace(source, destination):
        temporary = Path(source)
        assert temporary.parent == spool
        assert temporary.name.startswith(".tmp-")
        assert temporary.read_bytes() == b'{"synthetic":true}'
        observed.append(temporary)
        _assert_release_clean(repo, env)
        return os.replace(source, destination)

    isolated_os = SimpleNamespace(**{**vars(os), "replace": inspect_before_replace})
    monkeypatch.setattr(webhook_routes, "os", isolated_os)
    webhook_routes._atomic_write(spool, target, b'{"synthetic":true}')

    assert len(observed) == 1
    assert not observed[0].exists()
    assert target.read_bytes() == b'{"synthetic":true}'
    _assert_release_clean(repo, env)


def test_actual_worker_spool_probe_keeps_git_and_release_guard_clean(
    clean_repo, monkeypatch,
):
    from services import worker_health

    repo, env = clean_repo
    spool = repo / "data" / "webhook_spool"
    monkeypatch.setenv("WEBHOOK_SPOOL_DIR", str(spool))
    observed = []

    def inspect_before_unlink(name):
        temporary = Path(name)
        assert temporary.parent == spool
        assert temporary.name.startswith(".health-")
        assert temporary.read_bytes() == b"ok"
        observed.append(temporary)
        _assert_release_clean(repo, env)
        return os.unlink(name)

    isolated_os = SimpleNamespace(**{**vars(os), "unlink": inspect_before_unlink})
    monkeypatch.setattr(worker_health, "os", isolated_os)
    assert worker_health.probe_spool_writable() is True

    assert len(observed) == 1
    assert not observed[0].exists()
    _assert_release_clean(repo, env)


@pytest.mark.parametrize("relative", [
    "data/webhook_spool/.tmp-synthetic",
    "data/webhook_spool/.health-synthetic",
    "data/webhook_spool/inflight/.tmp-synthetic",
    "data/webhook_spool/dead-letter/.health-synthetic",
    "data/webhook_spool/inflight/claimed.json",
    "data/webhook_spool/dead-letter/failed.json",
    "data/webhook_spool/.lock",
    "data/webhook_spool/.completed-digests",
    "data/webhook_spool/queued.json",
])
def test_runtime_spool_paths_remain_ignored(clean_repo, relative):
    repo, env = clean_repo
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic runtime data\n", encoding="utf-8")
    _assert_release_clean(repo, env)


@pytest.mark.parametrize("relative", [
    ".tmp-synthetic",
    ".health-synthetic",
    "other/.tmp-synthetic",
    "other/.health-synthetic",
    "data/other/.tmp-synthetic",
    "data/other/.health-synthetic",
    "nested/data/webhook_spool/.tmp-synthetic",
    "nested/data/webhook_spool/.health-synthetic",
    "data/webhook_spool/other/.tmp-synthetic",
    "data/webhook_spool/other/.health-synthetic",
    "data/webhook_spool/.tmp",
    "data/webhook_spool/.health",
    "data/webhook_spool/.tmp_source",
    "data/webhook_spool/.health_source",
    "data/webhook_spool/unexpected.txt",
    "data/webhook_spool/repair.py",
    "untracked_source.py",
    "core/untracked_source.py",
])
def test_unrelated_or_unrecognized_files_still_block_release(clean_repo, relative):
    repo, env = clean_repo
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not an approved runtime file\n", encoding="utf-8")
    _assert_release_dirty(repo, env, relative)


@pytest.mark.parametrize("relative", [
    "tracked.py",
    "data/webhook_spool/.tmp-tracked.py",
    "data/webhook_spool/.health-tracked.py",
])
def test_tracked_changes_are_never_hidden_by_runtime_ignore(clean_repo, relative):
    repo, env = clean_repo
    target = repo / relative
    if relative != "tracked.py":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original = True\n", encoding="utf-8")
        _git(repo, env, "add", "--force", relative)
        _commit(repo, env)
    target.write_text("changed = True\n", encoding="utf-8")
    _assert_release_dirty(repo, env, relative)
