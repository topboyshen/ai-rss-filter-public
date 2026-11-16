"""The CI-only index crosses sudo as one fixed value, never ambient authority."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

import test_production_release_adapter_workflow_paths as workflow_guard


ROOT = Path(__file__).resolve().parents[1]
ASSIGNMENT = "STARTUP_PIP_INDEX_URL=https://pypi.org/simple"


def activation_run():
    workflow = yaml.load((ROOT / workflow_guard.PATH).read_text(), Loader=yaml.BaseLoader)
    return workflow_guard._full_activation_step(workflow)["run"]


def activation_args(run, tmp_path):
    tree = workflow_guard._full_activation_tree(run)
    expression = workflow_guard._single_assignment(tree, "args").value
    return eval(compile(ast.Expression(expression), "<actual-activation-argv>", "eval"), {
        "root": tmp_path, "sys": sys,
        "os": SimpleNamespace(environ={"AI_FILTER_NATIVE_BOOTSTRAP_IMAGE": "sha256:" + "a" * 64,
                                       "STARTUP_PIP_INDEX_URL": "https://ambient.invalid/simple"}),
    })


def test_full_activation_pins_only_the_public_index_after_sudo(tmp_path):
    run = activation_run()
    workflow_guard._assert_full_activation_contract(run)
    args = activation_args(run, tmp_path)
    assert args[:8] == ["/usr/bin/sudo", "-n", "--", "/usr/bin/env", ASSIGNMENT, sys.executable, "-I", "-B"]
    assert args[8] == str(tmp_path / "source/scripts/production_activation_host_native.py")
    assert args.count(ASSIGNMENT) == 1
    assert not any(value in {"-E", "--preserve-env"} or value.startswith("--preserve-env=") for value in args)


@pytest.mark.parametrize("ambient", [None, "https://ambient.invalid/simple"])
def test_fixed_post_sudo_assignment_reaches_real_build_argument_helper(tmp_path, ambient):
    args = activation_args(activation_run(), tmp_path)
    assert args[3:6] == ["/usr/bin/env", ASSIGNMENT, sys.executable]
    # Simulate only the environment boundary, not sudo permission or Docker.
    # Execute the actual env prefix and exact helper AST, with no network/import
    # of the application and no credentials inherited from the test process.
    environment = {"PATH": os.defpath}
    if ambient is not None:
        environment["STARTUP_PIP_INDEX_URL"] = ambient
    code = """
import ast, json, os, pathlib, sys, types, urllib.parse
source = pathlib.Path(sys.argv[1]).read_text()
node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
            and n.name == 'pip_index_build_args')
def require(value, message):
    if not value:
        raise ValueError(message)
namespace = {'urllib': urllib, 'private': types.SimpleNamespace(require=require)}
exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-index-helper>', 'exec'), namespace)
print(json.dumps(namespace['pip_index_build_args'](os.environ)))
"""
    result = subprocess.run([*args[3:6], "-I", "-B", "-c", code,
                             str(ROOT / "scripts/verify_production_startup_native.py")],
                            env=environment, capture_output=True, text=True, timeout=5, check=True)
    assert not result.stderr
    assert json.loads(result.stdout) == ["--build-arg", "PIP_INDEX_URL=https://pypi.org/simple"]


@pytest.mark.parametrize("before,after", [
    ("'/usr/bin/env', 'STARTUP_PIP_INDEX_URL=https://pypi.org/simple', ", ""),
    ("https://pypi.org/simple", "https://other.invalid/simple"),
    ("https://pypi.org/simple", "http://pypi.org/simple"),
    ("https://pypi.org/simple", "https://user:secret@pypi.org/simple"),
    ("'STARTUP_PIP_INDEX_URL=https://pypi.org/simple'", "'STARTUP_PIP_INDEX_URL=' + os.environ['STARTUP_PIP_INDEX_URL']"),
    ("'/usr/bin/env',", "'/usr/bin/env', 'DOCKER_HOST=unix:///tmp/unreviewed.sock',"),
    ("'/usr/bin/env'", "'/bin/sh'"),
    ("'/usr/bin/sudo', '-n', '--'", "'/usr/bin/sudo', '-E', '-n', '--'"),
    ("'/usr/bin/sudo', '-n', '--'", "'/usr/bin/sudo', '--preserve-env', '-n', '--'"),
    ("'STARTUP_PIP_INDEX_URL=https://pypi.org/simple',", "'STARTUP_PIP_INDEX_URL=https://pypi.org/simple', 'STARTUP_PIP_INDEX_URL=https://other.invalid/simple',"),
])
def test_exact_workflow_guard_rejects_index_transport_drift_even_with_decoy(before, after):
    run = activation_run()
    block = next(block for block in re.findall(r"python -I -B - <<'PY'\n(.*?)^PY$", run, re.MULTILINE | re.DOTALL)
                 if "'ACTIVATION_HOST_PROCESS'" in block)
    assert before in block
    changed = run.replace(block, block.replace(before, after, 1) + "\n# expected-text-decoy: " + before + "\n", 1)
    assert before in changed
    with pytest.raises(AssertionError):
        workflow_guard._assert_full_activation_contract(changed)


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_index_transport_regression_selects_the_native_workflow(event):
    workflow = yaml.load((ROOT / workflow_guard.PATH).read_text(), Loader=yaml.BaseLoader)
    assert workflow_guard.selected("tests/" + Path(__file__).name, workflow["on"][event]["paths"])
