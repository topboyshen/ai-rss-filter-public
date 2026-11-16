"""The isolated full suite gets one explicit Node executable, not an ambient PATH."""

import json
from pathlib import Path
import shlex

import pytest
import yaml

from test_release_tests_runner import (
    PROJECT_ROOT,
    RUNNER,
    _make_fake_python,
    _make_runner_repo,
    _run,
    _runner_env,
)


SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _node(tmp_path):
    directory = tmp_path / "toolcache" / "bin"
    directory.mkdir(parents=True, mode=0o755)
    executable = directory / "node"
    executable.write_text("#!/bin/sh\nprintf 'fixture-node-v24.21.0\\n'\n")
    executable.chmod(0o755)
    sibling = directory / "ai-filter-unrelated-tool"
    sibling.write_text("#!/bin/sh\nexit 88\n")
    sibling.chmod(0o755)
    return executable


def _case(tmp_path):
    repo = _make_runner_repo(tmp_path)
    tmp_base = tmp_path / "runner-tmp"
    tmp_base.mkdir()
    fake = _make_fake_python(tmp_path)
    node = _node(tmp_path)
    env = _runner_env(fake["executable"], tmp_base)
    env["AI_FILTER_TEST_NODE"] = str(node.resolve())
    env["NODE_OPTIONS"] = "--require /must-not-load-ambient.js"
    env["NODE_PATH"] = "/must-not-load-ambient-modules"
    env["PATH"] = f"{node.parent}:{env['PATH']}"
    return repo, tmp_base, fake, node, env


def test_full_mode_exposes_only_one_node_through_private_cleaned_directory(tmp_path):
    repo, tmp_base, fake, node, env = _case(tmp_path)
    selected = tmp_path / "selected-node"
    version = tmp_path / "selected-version"
    link_target = tmp_path / "selected-target"
    directory_mode = tmp_path / "node-bin-mode"
    with fake["executable"].open("a") as stream:
        stream.write(
            f'command -v node > {shlex.quote(str(selected))}\n'
            f'node --version > {shlex.quote(str(version))}\n'
            f'readlink "$(command -v node)" > {shlex.quote(str(link_target))}\n'
            'if command -v ai-filter-unrelated-tool >/dev/null 2>&1; then exit 89; fi\n'
            'test "$(ls -A "$(dirname "$(command -v node)")")" = node\n'
            'node_dir="$(dirname "$(command -v node)")"\n'
            f'(stat -c %a "$node_dir" 2>/dev/null || stat -f %Lp "$node_dir") > {shlex.quote(str(directory_mode))}\n'
        )
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    child = dict(line.split("=", 1) for line in fake["environment"].read_text().splitlines())
    private_bin = Path(selected.read_text().strip()).parent
    assert private_bin.name == "node-bin"
    assert private_bin.parent.parent == tmp_base.resolve()
    assert child["PATH"] == f"{private_bin}:{SAFE_PATH}"
    assert str(node.parent) not in child["PATH"]
    assert version.read_text() == "fixture-node-v24.21.0\n"
    assert link_target.read_text().strip() == str(node.resolve())
    assert directory_mode.read_text().strip() == "700"
    assert all(name not in child for name in ("AI_FILTER_TEST_NODE", "NODE_OPTIONS", "NODE_PATH"))
    assert not private_bin.exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize("mode", ["focused", "full"])
def test_no_explicit_full_node_keeps_original_fixed_path(tmp_path, mode):
    repo, tmp_base, fake, _, env = _case(tmp_path)
    if mode == "full":
        env.pop("AI_FILTER_TEST_NODE")
    else:
        env["AI_FILTER_TEST_NODE"] = "/nonexistent/ignored-in-focused-mode"
    result = _run([str(repo / "scripts" / RUNNER.name), mode], cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    child = dict(line.split("=", 1) for line in fake["environment"].read_text().splitlines())
    assert child["PATH"] == SAFE_PATH
    assert "NODE_OPTIONS" not in child and "NODE_PATH" not in child
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize(
    "invalid",
    ["relative", "directory", "missing", "non_executable", "symlink", "newline", "carriage_return", "group_writable", "world_writable", "source", "source_parent_alias"],
)
def test_unsafe_explicit_node_is_rejected_before_creating_worktree(tmp_path, invalid):
    repo, tmp_base, fake, node, env = _case(tmp_path)
    target = str(node.resolve())
    if invalid == "relative":
        target = "toolcache/bin/node"
    elif invalid == "directory":
        target = str(node.parent.resolve())
    elif invalid == "missing":
        target = str(tmp_path / "missing-node")
    elif invalid == "non_executable":
        node.chmod(0o644)
    elif invalid == "symlink":
        alias = tmp_path / "node-alias"
        alias.symlink_to(node)
        target = str(alias)
    elif invalid in {"newline", "carriage_return"}:
        target += "\n" if invalid == "newline" else "\r"
    elif invalid == "group_writable":
        node.chmod(0o775)
    elif invalid == "world_writable":
        node.chmod(0o757)
    else:
        internal = repo / "secrets" / "node"
        internal.parent.mkdir()
        internal.write_bytes(node.read_bytes())
        internal.chmod(0o755)
        target = str(internal)
        if invalid == "source_parent_alias":
            alias = tmp_path / "source-alias"
            alias.symlink_to(internal.parent, target_is_directory=True)
            target = str(alias / "node")
    env["AI_FILTER_TEST_NODE"] = target
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 2, result.stderr
    assert "AI_FILTER_TEST_NODE" in result.stderr
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_explicit_node_does_not_bypass_dirty_source_gate(tmp_path):
    repo, tmp_base, fake, _, env = _case(tmp_path)
    (repo / "uncommitted.txt").write_text("dirty")
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 2
    assert "dirty source worktree" in result.stderr
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_explicit_node_does_not_bypass_required_coverage_or_cleanup(tmp_path):
    repo, tmp_base, _, node, env = _case(tmp_path)
    fake = _make_fake_python(tmp_path, write_coverage=False)
    env["AI_FILTER_TEST_PYTHON"] = str(fake["executable"].resolve())
    env["AI_FILTER_TEST_NODE"] = str(node.resolve())
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 1
    assert "did not produce a regular coverage.xml" in result.stderr
    assert fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_explicit_node_rejects_colon_tmpdir_before_path_can_execute_unrelated_tool(tmp_path):
    repo, _, fake, _, env = _case(tmp_path)
    injected_bin = tmp_path / "unrelated-bin"
    injected_bin.mkdir()
    injected_node = injected_bin / "node"
    injected_node.write_text("#!/bin/sh\nprintf 'INJECTED UNRELATED NODE\\n'\n")
    injected_node.chmod(0o755)
    # This is one valid directory name, but PATH would interpret the colon as a separator.
    tmp_base = Path(f"{injected_bin}:{tmp_path / 'second-component'}")
    tmp_base.mkdir(parents=True)
    observed = tmp_path / "observed-node"
    with fake["executable"].open("a") as stream:
        stream.write(f'node --version > {shlex.quote(str(observed))}\n')
    env["AI_FILTER_TEST_TMPDIR"] = str(tmp_base)
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    actual = observed.read_text() if observed.exists() else "test executable not run"
    assert result.returncode == 2, actual
    assert "AI_FILTER_TEST_TMPDIR" in result.stderr
    assert not observed.exists()
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize("separator", ["\n", "\r"])
def test_explicit_node_rejects_control_characters_in_tmpdir(tmp_path, separator):
    repo, _, fake, _, env = _case(tmp_path)
    tmp_base = tmp_path / f"runner{separator}tmp"
    tmp_base.mkdir()
    env["AI_FILTER_TEST_TMPDIR"] = str(tmp_base)
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 2, result.stderr
    assert "AI_FILTER_TEST_TMPDIR" in result.stderr
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_explicit_node_rejects_tmpdir_alias_hiding_path_separator(tmp_path):
    repo, _, fake, _, env = _case(tmp_path)
    tmp_base = tmp_path / "hidden:separator"
    tmp_base.mkdir()
    alias = tmp_path / "safe-looking-tmpdir"
    alias.symlink_to(tmp_base, target_is_directory=True)
    env["AI_FILTER_TEST_TMPDIR"] = str(alias)
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 2, result.stderr
    assert "AI_FILTER_TEST_TMPDIR" in result.stderr
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize("suffix", ["\n", "\n\n", "\r\n"], ids=["lf", "double-lf", "crlf"])
def test_explicit_node_rejects_tmpdir_alias_hiding_trailing_linebreak(tmp_path, suffix):
    repo, _, fake, _, env = _case(tmp_path)
    original = tmp_path / f"actual-temp{suffix}"
    original.mkdir()
    neighbor = tmp_path / "actual-temp"
    neighbor.mkdir()
    alias = tmp_path / "safe-temp-alias"
    alias.symlink_to(original, target_is_directory=True)
    env["AI_FILTER_TEST_TMPDIR"] = str(alias)
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    actual = fake["environment"].read_text() if fake["environment"].exists() else "test not run"
    assert result.returncode == 2, actual
    assert "AI_FILTER_TEST_TMPDIR" in result.stderr
    assert not fake["arguments"].exists()
    assert not list(original.glob("ai-filter-release-tests.*"))
    assert not list(neighbor.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize("suffix", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_explicit_node_rejects_parent_alias_hiding_trailing_linebreak(tmp_path, suffix):
    repo, tmp_base, fake, node, env = _case(tmp_path)
    original = tmp_path / f"actual-node{suffix}"
    original.mkdir()
    (original / "node").write_bytes(node.read_bytes())
    (original / "node").chmod(0o755)
    neighbor = tmp_path / "actual-node"
    neighbor.mkdir()
    substituted = neighbor / "node"
    substituted.write_text("#!/bin/sh\nprintf 'WRONG NEIGHBOR NODE\\n'\n")
    substituted.chmod(0o755)
    alias = tmp_path / "safe-node-alias"
    alias.symlink_to(original, target_is_directory=True)
    env["AI_FILTER_TEST_NODE"] = str(alias / "node")
    observed = tmp_path / "observed-node"
    with fake["executable"].open("a") as stream:
        stream.write(f'node --version > {shlex.quote(str(observed))}\n')
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    actual = observed.read_text() if observed.exists() else "test not run"
    assert result.returncode == 2, actual
    assert "AI_FILTER_TEST_NODE" in result.stderr
    assert not fake["arguments"].exists()
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_normal_directory_aliases_still_select_original_node_and_temp_root(tmp_path):
    repo, _, fake, node, env = _case(tmp_path)
    actual_temp = tmp_path / "normal temp"
    actual_temp.mkdir()
    temp_alias = tmp_path / "temp-alias"
    temp_alias.symlink_to(actual_temp, target_is_directory=True)
    node_alias = tmp_path / "node-dir-alias"
    node_alias.symlink_to(node.parent, target_is_directory=True)
    env["AI_FILTER_TEST_TMPDIR"] = str(temp_alias)
    env["AI_FILTER_TEST_NODE"] = str(node_alias / "node")
    observed = tmp_path / "observed-node"
    with fake["executable"].open("a") as stream:
        stream.write(f'node --version > {shlex.quote(str(observed))}\n')
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    assert observed.read_text() == "fixture-node-v24.21.0\n"
    child = dict(line.split("=", 1) for line in fake["environment"].read_text().splitlines())
    assert Path(child["HOME"]).parent.parent == actual_temp.resolve()
    assert child["PATH"].split(":", 1)[0].startswith(str(actual_temp.resolve()) + "/")
    assert not list(actual_temp.glob("ai-filter-release-tests.*"))


@pytest.mark.parametrize("mode", ["focused", "full"])
def test_colon_tmpdir_without_explicit_full_node_preserves_fixed_path_behavior(tmp_path, mode):
    repo, _, fake, _, env = _case(tmp_path)
    tmp_base = tmp_path / "existing:tmpdir"
    tmp_base.mkdir()
    env["AI_FILTER_TEST_TMPDIR"] = str(tmp_base)
    if mode == "full":
        env.pop("AI_FILTER_TEST_NODE")
    result = _run([str(repo / "scripts" / RUNNER.name), mode], cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    child = dict(line.split("=", 1) for line in fake["environment"].read_text().splitlines())
    assert child["PATH"] == SAFE_PATH
    assert not list(tmp_base.glob("ai-filter-release-tests.*"))


def test_ci_installs_exact_node_and_passes_only_executable_to_full_runner():
    workflow = yaml.safe_load((PROJECT_ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    node = [step for step in steps if step.get("uses", "").startswith("actions/setup-node@")]
    assert len(node) == 1
    assert node[0]["uses"] == "actions/setup-node@v6"
    assert node[0]["with"] == {"node-version": "24.21.0", "package-manager-cache": False}
    python_index = next(index for index, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))
    preparation_index = next(index for index, step in enumerate(steps) if step.get("id") == "isolated_node")
    suite_index = next(index for index, step in enumerate(steps) if step.get("name") == "Run isolated full test suite with coverage")
    assert python_index < steps.index(node[0]) < preparation_index < suite_index
    suite = steps[suite_index]["run"]
    assert 'AI_FILTER_TEST_NODE="${{ steps.isolated_node.outputs.path }}"' in suite
    assert "./scripts/run-release-tests.sh full" in suite
    assert "AI_FILTER_TEST_COVERAGE_OUTPUT=" in suite
    assert "AI_FILTER_CUTOVER_EVIDENCE_DIR=" in suite
    assert "NODE_OPTIONS" not in json.dumps(workflow)
