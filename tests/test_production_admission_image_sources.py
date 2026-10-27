"""Runtime source-byte dependencies must exist in the actual application layout."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from services import production_admission_lifecycle as life

ROOT = Path(__file__).resolve().parents[1]
_SHARED_INPUT = None


def file_record(path):
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            **{key: getattr(info, "st_" + key) for key in ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns")}}


def layout_record(root):
    files, directories = {}, {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        name = str(path.relative_to(root))
        assert not stat.S_ISLNK(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            directories[name] = {key: getattr(info, "st_" + key) for key in ("dev", "ino", "mode")}
        else:
            files[name] = file_record(path)
    return {"root": str(root), "root_identity": {key: getattr(root.lstat(), "st_" + key)
            for key in ("dev", "ino", "mode")}, "files": files, "directories": directories}


def save_record(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture(scope="module")
def shared_image_input(tmp_path_factory):
    retained = tmp_path_factory.mktemp("runtime-image-input")
    layout = retained / "layout"
    layout.mkdir(mode=0o700)
    image_layout(layout)
    for path in sorted(layout.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        assert not path.is_symlink()
        path.chmod(0o500 if path.is_dir() else 0o400)
    layout.chmod(0o500)
    record = layout_record(layout)
    digest = save_record(retained / "INPUT.json", record)
    return {"layout": layout, "record": record, "sha256": digest, "binding": retained / "INPUT.json"}


@pytest.fixture(autouse=True)
def bind_shared_image_input(shared_image_input, monkeypatch):
    # The legacy runner's direct helper call still creates its own actual COPY
    # layout. Only these seven collected tests share this retained real input.
    monkeypatch.setattr(sys.modules[__name__], "_SHARED_INPUT", shared_image_input)


def check_shared_input(value):
    assert hashlib.sha256(value["binding"].read_bytes()).hexdigest() == value["sha256"]
    assert layout_record(value["layout"]) == value["record"]
    assert all(stat.S_IMODE(row["mode"]) == 0o400 for row in value["record"]["files"].values())
    assert stat.S_IMODE(value["record"]["root_identity"]["mode"]) == 0o500


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
    if _SHARED_INPUT is None:
        image_layout(tmp_path)
        layout = tmp_path
    else:
        check_shared_input(_SHARED_INPUT)
        layout = _SHARED_INPUT["layout"]
    assert all((layout / path).is_file() for path in runtime_sources())
    result = image_digest(layout)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == life.source_sha256()
    if _SHARED_INPUT is not None:
        check_shared_input(_SHARED_INPUT)


def missing_projection(tmp_path, shared, missing):
    check_shared_input(shared)
    projection = tmp_path / "projection"
    projection.mkdir(mode=0o700)
    expected = set(runtime_sources()) - {missing}
    for name in sorted(expected):
        source, target = shared["layout"] / name, projection / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        assert file_record(target)["sha256"] == shared["record"]["files"][name]["sha256"]
        assert (target.stat().st_dev, target.stat().st_ino) != (source.stat().st_dev, source.stat().st_ino)
    record = layout_record(projection)
    assert set(record["files"]) == expected and not (projection / missing).exists()
    save_record(tmp_path / "PROJECTION.json", {"shared_input_sha256": shared["sha256"],
                "missing": missing, "expected_missing_path": str(projection / missing), "actual": record})
    return projection, record


PROJECTED_PROGRAM = """
import importlib.util, json, pathlib, sys, traceback
base, projection = map(pathlib.Path, sys.argv[1:3])
sys.path.insert(0, str(base))
path = projection / 'services/production_admission_lifecycle.py'
spec = importlib.util.spec_from_file_location('actual_projected_lifecycle', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert pathlib.Path(module.__file__) == path
assert pathlib.Path(module.source_sha256.__code__.co_filename) == path
try:
    module.source_sha256()
except FileNotFoundError as error:
    print(json.dumps({'exception': type(error).__name__, 'filename': error.filename,
                      'module_path': module.__file__}))
    traceback.print_exc()
    sys.exit(1)
raise AssertionError('missing actual hash input was accepted')
"""


@pytest.mark.parametrize("missing", [path for path in runtime_sources() if path.startswith("scripts/")])
def test_missing_one_runtime_hash_input_fails_closed(tmp_path, missing, shared_image_input):
    projection, record = missing_projection(tmp_path, shared_image_input, missing)
    args = [sys.executable, "-I", "-B", "-c", PROJECTED_PROGRAM, str(shared_image_input["layout"]), str(projection)]
    result = subprocess.run(args, cwd="/", capture_output=True, text=True, timeout=10, check=False)
    save_record(tmp_path / "RESULT.json", {"returncode": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "program_sha256": hashlib.sha256(PROJECTED_PROGRAM.encode()).hexdigest(),
                "shared_input_sha256": shared_image_input["sha256"], "projection": str(projection)})
    assert result.returncode == 1 and "FileNotFoundError" in result.stderr
    assert json.loads(result.stdout) == {"exception": "FileNotFoundError", "filename": str(projection / missing),
                                       "module_path": str(projection / "services/production_admission_lifecycle.py")}
    assert layout_record(projection) == record
    check_shared_input(shared_image_input)
