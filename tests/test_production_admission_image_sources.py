"""Runtime source-byte dependencies must exist in the actual application layout."""
import ast
from contextlib import contextmanager, ExitStack
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


def directory_identity(info):
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
    return {key: getattr(info, "st_" + key) for key in ("dev", "ino", "uid", "gid")}


def owned_layout(layout, stack):
    assert layout.is_absolute() and layout.resolve() == layout
    record = layout_record(layout)
    descriptors = {}
    parent = os.open(layout.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stack.callback(os.close, parent)
    parent_identity = directory_identity(os.fstat(parent))
    for name in ("", *record["directories"]):
        fd = os.open(layout / name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        identity = directory_identity(os.fstat(fd))
        expected = record["directories"][name] if name else record["root_identity"]
        assert all(identity[key] == expected[key] for key in ("dev", "ino"))
        descriptors[name] = (fd, identity)
    return {"layout": layout, "parent": parent, "parent_identity": parent_identity,
            "directories": descriptors, "files": record["files"]}


def check_owned_directories(owned):
    layout = owned["layout"]
    assert layout.resolve() == layout
    assert directory_identity(layout.parent.lstat()) == owned["parent_identity"]
    assert directory_identity(os.fstat(owned["parent"])) == owned["parent_identity"]
    for name, (fd, identity) in owned["directories"].items():
        assert directory_identity(os.fstat(fd)) == identity
        relative = Path(name)
        parent_name = "" if relative.parent == Path(".") else str(relative.parent)
        parent = owned["parent"] if not name else owned["directories"][parent_name][0]
        leaf = layout.name if not name else relative.name
        fresh = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            assert directory_identity(os.fstat(fresh)) == identity
        finally:
            os.close(fresh)


def freeze_owned_layout(owned):
    check_owned_directories(owned)
    for name, row in owned["files"].items():
        relative = Path(name)
        parent = "" if relative.parent == Path(".") else str(relative.parent)
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=owned["directories"][parent][0])
        try:
            info = os.fstat(fd)
            assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
            assert (info.st_dev, info.st_ino) == (row["dev"], row["ino"])
            os.fchmod(fd, 0o400)
        finally:
            os.close(fd)
    for fd, _identity in owned["directories"].values():
        os.fchmod(fd, 0o500)


def restore_owned_directories(owned):
    # Validate the whole set before changing any permissions. fchmod then acts
    # only on original held directory inodes, never a replaced path or symlink.
    check_owned_directories(owned)
    for fd, _identity in owned["directories"].values():
        os.fchmod(fd, 0o700)
    check_owned_directories(owned)


@contextmanager
def immutable_image_input(retained):
    layout = retained / "layout"
    layout.mkdir(mode=0o700)
    image_layout(layout)
    with ExitStack() as stack:
        owned = owned_layout(layout, stack)
        try:
            freeze_owned_layout(owned)
            record = layout_record(layout)
            digest = save_record(retained / "INPUT.json", record)
            yield {"layout": layout, "record": record, "sha256": digest, "binding": retained / "INPUT.json"}
        finally:
            restore_owned_directories(owned)


@pytest.fixture(scope="module")
def shared_image_input(tmp_path_factory):
    with immutable_image_input(tmp_path_factory.mktemp("runtime-image-input")) as value:
        yield value


@pytest.fixture(autouse=True)
def bind_shared_image_input(shared_image_input, monkeypatch):
    # The legacy runner's direct helper call still creates its own actual COPY
    # layout. Collected source-input tests share this retained real input.
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


def copy_image_directory(source, destination):
    # Do not inherit source directory modes before ownership has been captured:
    # a later missing COPY input must leave a normally cleanable partial setup.
    destination.mkdir(mode=0o700)
    for child in sorted(source.iterdir()):
        if child.name == "__pycache__":
            continue
        info = child.lstat()
        assert not stat.S_ISLNK(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            copy_image_directory(child, destination / child.name)
        else:
            assert stat.S_ISREG(info.st_mode)
            shutil.copyfile(child, destination / child.name)


def image_layout(tmp_path):
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        _copy, source, destination = line.split()
        destination = tmp_path / destination.removeprefix("./")
        if (ROOT / source).is_dir():
            copy_image_directory(ROOT / source, destination)
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
        with immutable_image_input(tmp_path) as value:
            assert_runtime_image(value)
    else:
        assert_runtime_image(_SHARED_INPUT)


def assert_runtime_image(value):
    check_shared_input(value)
    layout = value["layout"]
    assert all((layout / path).is_file() for path in runtime_sources())
    result = image_digest(layout)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == life.source_sha256()
    check_shared_input(value)


def test_retained_gateway_helpers_import_only_from_actual_copy_layout(shared_image_input):
    """No host controller, Docker access, or ambient checkout may supply imports."""
    value = shared_image_input
    check_shared_input(value)
    layout = value["layout"]
    names = ("retained_gateway_configuration", "retained_gateway_custody")
    assert all("scripts/" + name + ".py" in runtime_sources() for name in names)
    assert not (layout / "scripts/private_release_contract.py").exists()
    assert not (layout / "scripts/production_gateway_switch.py").exists()
    program = """
import importlib, json, pathlib, sys
layout = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(layout))
names = ('retained_gateway_configuration', 'retained_gateway_custody')
for name in names:
    module = importlib.import_module('scripts.' + name)
    assert pathlib.Path(module.__file__) == layout / 'scripts' / (name + '.py')
assert 'scripts.private_release_contract' not in sys.modules
assert 'scripts.production_gateway_switch' not in sys.modules
assert 'scripts.production_legacy_takeover' not in sys.modules
print(json.dumps({'imported': list(names), 'host_controller_loaded': False}))
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(layout)], cwd="/",
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"imported": list(names), "host_controller_loaded": False}
    check_shared_input(value)


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


def assert_cleanup_modes(layout):
    assert stat.S_IMODE(layout.stat().st_mode) == 0o700
    for path in layout.rglob("*"):
        assert not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o400)


def test_module_fixture_finalization_preserves_files_and_allows_real_rmtree(tmp_path):
    retained = tmp_path / "module-owned"
    retained.mkdir(mode=0o700)
    class Factory:
        def mktemp(self, _name):
            return retained
    generator = shared_image_input.__wrapped__(Factory())
    value = next(generator)
    check_shared_input(value)
    before = {name: (value["layout"] / name).read_bytes() for name in value["record"]["files"]}
    binding = value["binding"].read_bytes()
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            (value["layout"] / "not-allowed").write_bytes(b"must not be created")
    with pytest.raises(StopIteration):
        next(generator)
    assert_cleanup_modes(value["layout"])
    assert value["binding"].read_bytes() == binding
    assert {name: (value["layout"] / name).read_bytes() for name in before} == before
    assert {name: file_record(value["layout"] / name) for name in before} == value["record"]["files"]
    # Only this regression's disposable synthetic tree is removed. The module
    # finalizer itself never removes the shared input or original records.
    shutil.rmtree(retained)
    assert not retained.exists()


@pytest.mark.parametrize("failure", ["case", "missing_input", "binding_write"])
def test_readonly_input_restores_owned_dirs_when_case_or_setup_fails(tmp_path, monkeypatch, failure):
    retained = tmp_path / "failed-case"
    retained.mkdir(mode=0o700)
    if failure == "binding_write":
        def fail_record(*_args):
            raise OSError("synthetic original binding write failure")
        monkeypatch.setattr(sys.modules[__name__], "save_record", fail_record)
    expected = {"case": RuntimeError, "missing_input": AssertionError, "binding_write": OSError}[failure]
    with pytest.raises(expected):
        with immutable_image_input(retained) as value:
            check_shared_input(value)
            if failure == "case":
                raise RuntimeError("synthetic original case failure")
            monkeypatch.setattr(sys.modules[__name__], "runtime_sources", lambda: ["missing-required.py"])
            assert_runtime_image(value)
    assert_cleanup_modes(retained / "layout")
    assert all(path.is_file() for path in (retained / "layout").rglob("*.py"))
    shutil.rmtree(retained)
    assert not retained.exists()


def test_missing_copy_input_does_not_leave_inherited_readonly_directory_modes(tmp_path, monkeypatch):
    source, retained = tmp_path / "source", tmp_path / "partial"
    (source / "readonly").mkdir(parents=True)
    original = source / "readonly/original.py"
    original.write_bytes(b"public_original = True\n")
    original.chmod(0o400)
    (source / "readonly").chmod(0o500)
    (source / "Dockerfile").write_text("COPY readonly ./readonly\nCOPY missing.py .\n")
    before = (file_record(original), (source / "readonly").stat().st_mode)
    retained.mkdir(mode=0o700)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", source)
    try:
        with pytest.raises(FileNotFoundError) as caught:
            with immutable_image_input(retained):
                pytest.fail("missing actual COPY input was accepted")
        assert Path(caught.value.filename) == source / "missing.py"
        assert (retained / "layout/readonly/original.py").read_bytes() == original.read_bytes()
        assert (file_record(original), (source / "readonly").stat().st_mode) == before
        shutil.rmtree(retained)
        assert not retained.exists()
    finally:
        (source / "readonly").chmod(0o700)  # This regression owns its source fixture.


def test_direct_positive_guard_restores_its_own_copy_even_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_SHARED_INPUT", None)
    for fail in (False, True):
        retained = tmp_path / ("failed" if fail else "successful")
        retained.mkdir(mode=0o700)
        if fail:
            def failed_digest(_layout):
                raise RuntimeError("synthetic child failure")
            monkeypatch.setattr(sys.modules[__name__], "image_digest", failed_digest)
            with pytest.raises(RuntimeError, match="synthetic child failure"):
                test_runtime_controller_hash_matches_from_docker_copy_layout(retained)
        else:
            test_runtime_controller_hash_matches_from_docker_copy_layout(retained)
        assert_cleanup_modes(retained / "layout")
        shutil.rmtree(retained)
        assert not retained.exists()


@pytest.mark.parametrize("change", ["root_identity", "child_identity", "root_alias", "child_alias"])
def test_cleanup_rejects_directory_drift_without_chmod_of_foreign_targets(tmp_path, change):
    layout = tmp_path / "layout"
    (layout / "child").mkdir(parents=True)
    (layout / "child/original").write_bytes(b"original evidence")
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o750)
    (foreign / "canary").write_bytes(b"foreign original")
    with ExitStack() as stack:
        owned = owned_layout(layout, stack)
        freeze_owned_layout(owned)
        path = layout if change.startswith("root") else layout / "child"
        if path != layout:
            os.fchmod(owned["directories"][""][0], 0o700)  # Explicit tamper injection only.
        held = path.with_name(path.name + "-original")
        path.rename(held)
        if change.endswith("alias"):
            path.symlink_to(foreign, target_is_directory=True)
        else:
            path.mkdir(mode=0o750)
        foreign_before = foreign.stat()
        held_modes = {name: os.fstat(fd).st_mode for name, (fd, _identity) in owned["directories"].items()}
        try:
            with pytest.raises((AssertionError, OSError)):
                restore_owned_directories(owned)
            assert foreign.stat() == foreign_before and (foreign / "canary").read_bytes() == b"foreign original"
            assert {name: os.fstat(fd).st_mode for name, (fd, _identity) in owned["directories"].items()} == held_modes
        finally:
            path.unlink() if path.is_symlink() else path.rmdir()
            held.rename(path)
            restore_owned_directories(owned)
    assert (layout / "child/original").read_bytes() == b"original evidence"
    shutil.rmtree(layout)
    assert not layout.exists() and (foreign / "canary").read_bytes() == b"foreign original"


def test_cleanup_permission_failure_is_not_ignored(tmp_path, monkeypatch):
    layout = tmp_path / "layout"
    layout.mkdir(mode=0o700)
    (layout / "original").write_bytes(b"retained")
    with ExitStack() as stack:
        owned = owned_layout(layout, stack)
        freeze_owned_layout(owned)
        with monkeypatch.context() as patch:
            def fail_chmod(_fd, _mode):
                raise PermissionError("synthetic fchmod failure")
            patch.setattr(os, "fchmod", fail_chmod)
            with pytest.raises(PermissionError, match="synthetic fchmod failure"):
                restore_owned_directories(owned)
        assert (layout / "original").read_bytes() == b"retained" and stat.S_IMODE(layout.stat().st_mode) == 0o500
        restore_owned_directories(owned)
    shutil.rmtree(layout)
    assert not layout.exists()
