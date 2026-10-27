"""Runtime source-byte dependencies must exist in the actual application layout."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from services import production_admission_lifecycle as life

ROOT = Path(__file__).resolve().parents[1]


def runtime_sources():
    tree = ast.parse((ROOT / "services/production_admission_lifecycle.py").read_text())
    function = next(row for row in tree.body if isinstance(row, ast.FunctionDef) and row.name == "source_sha256")
    assignment = next(row for row in function.body if isinstance(row, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "names" for target in row.targets))
    return ast.literal_eval(assignment.value)


def image_layout(tmp_path):
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        _copy, source, destination = line.split()
        destination = tmp_path / destination.removeprefix("./")
        if (ROOT / source).is_dir():
            shutil.copytree(ROOT / source, destination, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            if destination.is_dir():
                destination = destination / Path(source).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, destination)


def image_digest(tmp_path):
    program = ("import sys,json;sys.path.insert(0,sys.argv[1]);"
               "from services.production_admission_lifecycle import source_sha256;"
               "print(json.dumps(source_sha256()))")
    return subprocess.run([sys.executable, "-I", "-B", "-c", program, str(tmp_path)], cwd="/",
                          capture_output=True, text=True, timeout=10, check=False)


def test_runtime_controller_hash_matches_from_docker_copy_layout(tmp_path):
    image_layout(tmp_path)
    assert all((tmp_path / path).is_file() for path in runtime_sources())
    result = image_digest(tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == life.source_sha256()


@pytest.mark.parametrize("missing", [path for path in runtime_sources() if path.startswith("scripts/")])
def test_missing_one_runtime_hash_input_fails_closed(tmp_path, missing):
    image_layout(tmp_path)
    (tmp_path / missing).unlink()
    result = image_digest(tmp_path)
    assert result.returncode != 0 and "FileNotFoundError" in result.stderr
