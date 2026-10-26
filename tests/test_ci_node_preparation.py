"""Execute the actual hosted Node preparation shell against synthetic tools."""

import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess

import pytest
import yaml

from test_release_tests_runner import (
    PROJECT_ROOT, RUNNER, _make_fake_python, _make_runner_repo, _run, _runner_env,
)


def _preparation(tmp_path, *, mode=0o755, version="v24.21.0"):
    workflow = yaml.safe_load((PROJECT_ROOT / ".github/workflows/ci.yml").read_text())
    step = next(row for row in workflow["jobs"]["test"]["steps"]
                if row.get("id") == "isolated_node")
    assert step["shell"] == "bash"
    cache = tmp_path / "toolcache"
    node = cache / "node/24.21.0/x64/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text(
        "#!/bin/sh\nset -eu\n"
        'test -z "${NODE_OPTIONS+x}"\n'
        'test -z "${NODE_PATH+x}"\n'
        'test -z "${SYNTHETIC_PRIVATE_VALUE+x}"\n'
        f"printf '%s\\n' {shlex.quote(version)}\n"
    )
    node.chmod(mode)
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    output = tmp_path / "github-output"
    env = os.environ.copy()
    env.update(RUNNER_TOOL_CACHE=str(cache), RUNNER_TEMP=str(temp), GITHUB_OUTPUT=str(output),
               PATH=str(node.parent) + os.pathsep + env["PATH"],
               NODE_OPTIONS="--must-not-inherit", NODE_PATH="/not-a-real-module-path",
               SYNTHETIC_PRIVATE_VALUE="fixture-only")
    return step["run"], node, temp, output, env


def _execute(script, env):
    return subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                          env=env, capture_output=True, text=True, timeout=10, check=False)


@pytest.mark.parametrize("mode", [0o755, 0o775, 0o777])
def test_actual_workflow_prepares_private_identical_node_without_changing_cache(tmp_path, mode):
    script, source, temp, output, env = _preparation(tmp_path, mode=mode)
    original = source.read_bytes()
    result = _execute(script, env)
    assert result.returncode == 0, result.stderr
    private = Path(output.read_text().removeprefix("path=").strip())
    assert private.parent.parent == temp
    assert private.read_bytes() == original and not private.is_symlink()
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    assert private.stat().st_uid == private.parent.stat().st_uid == os.getuid()
    assert source.read_bytes() == original
    assert stat.S_IMODE(source.stat().st_mode) == mode
    assert f"setup-node mode={mode:o}, private mode=700" in result.stdout


@pytest.mark.parametrize("kind", ["outside_cache", "symlink", "parent_alias", "wrong_version",
                                  "non_executable", "directory"])
def test_wrong_tool_source_or_version_cannot_publish_an_output(tmp_path, kind):
    script, source, _, output, env = _preparation(
        tmp_path, version="v22.0.0" if kind == "wrong_version" else "v24.21.0")
    if kind == "outside_cache":
        env["RUNNER_TOOL_CACHE"] = str(tmp_path / "different-cache")
    elif kind in {"symlink", "parent_alias"}:
        original = source if kind == "symlink" else source.parent
        target = tmp_path / "other-tool"
        original.rename(target)
        original.symlink_to(target, target_is_directory=kind == "parent_alias")
    elif kind == "non_executable":
        source.chmod(0o644)
    elif kind == "directory":
        source.unlink()
        source.mkdir()
    result = _execute(script, env)
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("tamper", ["source", "copy", "install_failure"])
def test_changed_source_or_copy_fails_hash_check_before_output(tmp_path, tamper):
    script, source, _, output, env = _preparation(tmp_path)
    real_install = shutil.which("install")
    assert real_install
    commands = tmp_path / "commands"
    commands.mkdir()
    shim = commands / "install"
    target = shlex.quote(str(source)) if tamper == "source" else '"${!#}"'
    shim.write_text(
        "#!/bin/bash\nset -eu\n"
        f'{shlex.quote(real_install)} "$@"\n'
        f"printf '# changed\\n' >> {target}\n"
    )
    if tamper == "install_failure":
        shim.write_text("#!/bin/bash\nexit 19\n")
    shim.chmod(0o755)
    env["PATH"] = str(commands) + os.pathsep + env["PATH"]
    result = _execute(script, env)
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("bad_temp", ["relative", "colon:path", "newline\npath", "carriage\rpath"])
def test_temp_paths_cannot_inject_path_or_output_fields(tmp_path, bad_temp):
    script, _, _, output, env = _preparation(tmp_path)
    env["RUNNER_TEMP"] = bad_temp if bad_temp == "relative" else str(tmp_path / bad_temp)
    result = _execute(script, env)
    assert result.returncode != 0
    assert not output.exists()


def test_private_copy_is_accepted_by_unchanged_strict_suite_launcher(tmp_path):
    script, _, _, output, env = _preparation(tmp_path, mode=0o777)
    prepared = _execute(script, env)
    assert prepared.returncode == 0, prepared.stderr
    private = Path(output.read_text().removeprefix("path=").strip())
    repo = _make_runner_repo(tmp_path)
    scratch = tmp_path / "suite-temp"
    scratch.mkdir()
    fake = _make_fake_python(tmp_path)
    observed = tmp_path / "suite-node-version"
    with fake["executable"].open("a") as stream:
        stream.write(f'node --version > {shlex.quote(str(observed))}\n')
    suite_env = _runner_env(fake["executable"], scratch)
    suite_env["AI_FILTER_TEST_NODE"] = str(private)
    result = _run([str(repo / "scripts" / RUNNER.name), "full"], cwd=repo, env=suite_env)
    assert result.returncode == 0, result.stderr
    assert observed.read_text() == "v24.21.0\n"
