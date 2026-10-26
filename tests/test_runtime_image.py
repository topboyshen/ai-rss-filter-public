import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest
import yaml

import scripts.check_runtime_image as checker


ROOT = Path(__file__).resolve().parents[1]
DEBIAN_INSTALLED_VERSIONS = {
    "libc6": "2.41-12+deb13u4",
    "gzip": "1.13-1+deb13u1",
    "openssl": "3.5.7-1~deb13u3",
    "libpcre2-8-0": "10.46-1~deb13u3",
    "perl-base": "5.40.1-6+deb13u1",
    "libsqlite3-0": "3.46.1-7+deb13u2",
    "libuuid1": "2.41.5-0+deb13u1",
    "liblzma5": "5.8.1-1+deb13u1",
}


def _distribution(name, version="1.0", requires=None):
    return SimpleNamespace(
        metadata={"Name": name}, version=version, requires=requires,
    )


@pytest.fixture
def image_paths(tmp_path):
    paths = {name: tmp_path / name for name in ("site", "stdlib", "bin")}
    for path in paths.values():
        path.mkdir()
    return paths


def _packaging_errors(paths, *, versions=None, find_spec=lambda _name: None):
    return checker.packaging_errors(
        versions or {}, {paths["site"]}, paths["stdlib"], paths["bin"],
        find_spec=find_spec,
    )


def _debian_query_output(versions=DEBIAN_INSTALLED_VERSIONS):
    return "".join(f"{name}:amd64\t{version}\n" for name, version in versions.items())


def _debian_runner(stdout=None, *, query_returncode=0, compare_returncode=0):
    calls = []

    def run(command, **options):
        calls.append((command, options))
        if command[0] == "dpkg-query":
            return SimpleNamespace(
                returncode=query_returncode,
                stdout=_debian_query_output() if stdout is None else stdout,
            )
        assert command[:2] == ["dpkg", "--compare-versions"]
        return SimpleNamespace(returncode=compare_returncode)

    return run, calls


def test_debian_versions_check_all_eight_packages_with_debian_comparison():
    run, calls = _debian_runner()

    assert checker.debian_version_errors(run=run) == []

    query, options = calls[0]
    assert query[:3] == ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"]
    assert set(query[3:]) == set(DEBIAN_INSTALLED_VERSIONS)
    assert len(query[3:]) == 8
    assert options == {"capture_output": True, "text": True, "timeout": 5, "check": False}
    assert len(calls) == 9
    assert {tuple(command) for command, _options in calls[1:]} == {
        ("dpkg", "--compare-versions", version, "ge", version)
        for version in DEBIAN_INSTALLED_VERSIONS.values()
    }


def test_debian_missing_query_row_fails_even_when_query_exits_successfully():
    remaining = {
        name: version for name, version in DEBIAN_INSTALLED_VERSIONS.items()
        if name != "gzip"
    }
    run, calls = _debian_runner(_debian_query_output(remaining))

    assert checker.debian_version_errors(run=run) == ["Debian package missing: gzip"]
    assert len(calls) == 8


@pytest.mark.parametrize("stdout", [
    "libc6 2.41-12+deb13u4\n",
    "libc6\t2.41-12+deb13u4\tunexpected-field\n",
])
def test_debian_malformed_query_output_fails_closed(stdout):
    run, calls = _debian_runner(stdout)

    assert checker.debian_version_errors(run=run) == [
        "Debian installed-version check failed",
    ]
    assert len(calls) == 1


def test_debian_query_nonzero_is_rejected_before_comparing_versions():
    run, calls = _debian_runner(query_returncode=1)

    assert checker.debian_version_errors(run=run) == [
        "Debian installed-version query failed",
    ]
    assert len(calls) == 1


@pytest.mark.parametrize("returncode", [1, 2])
def test_debian_old_or_unverifiable_version_fails(returncode):
    run, calls = _debian_runner(compare_returncode=returncode)

    errors = checker.debian_version_errors(run=run)

    assert len(errors) == 8
    assert len(calls) == 9
    for name, version in DEBIAN_INSTALLED_VERSIONS.items():
        assert any(
            error.startswith(f"Debian fix floor unmet or unverifiable: {name} {version}")
            for error in errors
        )


@pytest.mark.parametrize("command", ["dpkg-query", "dpkg"])
@pytest.mark.parametrize("exception", [
    FileNotFoundError("Debian command unavailable"),
    subprocess.TimeoutExpired("Debian command", 5),
])
def test_debian_query_or_comparison_command_exception_fails_closed(command, exception):
    successful_run, calls = _debian_runner()

    def run(arguments, **options):
        if arguments[0] == command:
            raise exception
        return successful_run(arguments, **options)

    assert checker.debian_version_errors(run=run) == [
        "Debian installed-version check failed",
    ]
    assert len(calls) == (0 if command == "dpkg-query" else 1)


@pytest.mark.parametrize("versions", [{}, {"flask": "3.1.2"}])
def test_production_pin_rejects_missing_or_different_version(versions):
    errors = checker.dependency_errors(
        ["# The production lock must survive tool removal.", "", "Flask==3.1.3"],
        versions, [],
    )

    assert len(errors) == 1
    assert "production lock: Flask==3.1.3" in errors[0]
    assert f"installed={versions.get('flask')}" in errors[0]


def test_production_pin_accepts_normalized_distribution_names():
    assert checker.dependency_errors(
        ["typing_extensions==4.16.0", "Flask==3.1.3"],
        {"typing-extensions": "4.16.0", "flask": "3.1.3"}, [],
    ) == []


@pytest.mark.parametrize("versions", [{}, {"pydantic-core": "2.40.0"}])
def test_transitive_runtime_dependency_is_checked_even_without_a_lock_pin(versions):
    distributions = [
        _distribution("pydantic", requires=["pydantic_core==2.41.0"]),
    ]

    errors = checker.dependency_errors([], versions, distributions)

    assert len(errors) == 1
    assert "pydantic: pydantic_core==2.41.0" in errors[0]


def test_active_dependencies_are_required_but_unselected_extras_are_not():
    distributions = [
        _distribution("openai", requires=[
            'httpx>=0.28; python_version >= "3.8"',
            'aiohttp>=3.10; extra == "aiohttp"',
            'obsolete-helper; python_version < "3.0"',
        ]),
        _distribution("no-dependencies"),
    ]

    errors = checker.dependency_errors([], {}, distributions)

    assert len(errors) == 1
    assert errors[0].startswith("openai: httpx>=0.28")
    assert checker.dependency_errors(
        [], {"httpx": "0.28.1"}, distributions,
    ) == []


def test_installed_versions_normalizes_names(monkeypatch):
    monkeypatch.setattr(checker.importlib.metadata, "distributions", lambda: [
        _distribution("Flask", "3.1.3"),
        _distribution("pydantic_core", "2.41.0"),
    ])

    assert checker.installed_versions() == {
        "flask": "3.1.3", "pydantic-core": "2.41.0",
    }


@pytest.mark.parametrize("name", ["pip", "setuptools", "wheel"])
def test_packaging_distribution_metadata_is_rejected(image_paths, name):
    errors = _packaging_errors(image_paths, versions={name: "1.0"})

    assert f"packaging distribution remains: {name}" in errors


@pytest.mark.parametrize("module", [
    "pip", "setuptools", "wheel", "pkg_resources", "ensurepip", "_distutils_hack",
])
def test_importable_packaging_module_is_rejected_without_metadata(image_paths, module):
    errors = _packaging_errors(
        image_paths, find_spec=lambda name: object() if name == module else None,
    )

    assert errors == [f"packaging module remains: {module}"]


@pytest.mark.parametrize("relative_path", [
    "pip/_vendor/msgpack", "pip/_vendor/urllib3", "pip/_vendor/setuptools",
    "setuptools/_vendor/wheel", "setuptools/_vendor/jaraco/context",
    "pkg_resources/_vendor", "wheel", "_distutils_hack", "ensurepip/_bundled",
])
def test_nonimportable_packaging_trees_are_rejected_without_metadata(
    image_paths, relative_path,
):
    residual = image_paths["site"] / relative_path
    residual.mkdir(parents=True)

    errors = _packaging_errors(image_paths)

    assert any("packaging tree remains:" in error for error in errors)


def test_ensurepip_bundled_wheel_is_rejected_outside_site_packages(image_paths):
    archive = image_paths["stdlib"] / "ensurepip/_bundled/pip-26.2.0-py3-none-any.whl"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"a bundled wheel is not an installed distribution")

    assert _packaging_errors(image_paths) == [
        "ensurepip (including bundled wheels) remains",
    ]


@pytest.mark.parametrize("directory", [
    "pip-26.2.0.dist-info", "setuptools-79.0.0.dist-info", "wheel-0.46.2.dist-info",
])
def test_orphaned_packaging_dist_info_is_rejected(image_paths, directory):
    (image_paths["site"] / directory).mkdir()

    errors = _packaging_errors(image_paths)

    assert len(errors) == 1
    assert errors[0].startswith("packaging metadata remains:")


def test_setuptools_pth_hook_is_rejected_after_package_removal(image_paths):
    (image_paths["site"] / "distutils-precedence.pth").write_text(
        "import _distutils_hack\n", encoding="utf-8",
    )

    assert _packaging_errors(image_paths) == ["setuptools import hook remains"]


@pytest.mark.parametrize("executable", [
    "pip", "pip3", "pip3.11", "wheel", "easy_install", "easy_install-3.11",
])
def test_packaging_entrypoints_are_rejected_even_when_not_on_path(
    image_paths, executable,
):
    (image_paths["bin"] / executable).touch()

    assert _packaging_errors(image_paths) == [
        f"packaging executable remains: {executable}",
    ]


def test_application_vendor_and_packaging_library_are_allowed(image_paths):
    (image_paths["site"] / "openai/_vendor/httpx_aiohttp").mkdir(parents=True)
    (image_paths["site"] / "packaging").mkdir()
    (image_paths["site"] / "packaging-26.3.dist-info").mkdir()
    (image_paths["bin"] / "gunicorn").touch()

    assert _packaging_errors(
        image_paths, versions={"openai": "3.19.2", "packaging": "26.3"},
    ) == []


def test_runtime_imports_reference_existing_packaged_application_modules():
    for module in checker.RUNTIME_IMPORTS:
        if module.split(".")[0] in {"app", "worker_main", "workers", "modules", "scripts"}:
            assert (ROOT / (module.replace(".", "/") + ".py")).is_file(), module


def _isolated_main(monkeypatch, tmp_path, image_paths):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "requirements-prod.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(checker.importlib.metadata, "distributions", lambda: [])
    monkeypatch.setattr(checker.sysconfig, "get_paths", lambda: {
        "purelib": str(image_paths["site"]),
        "platlib": str(image_paths["site"]),
        "stdlib": str(image_paths["stdlib"]),
        "scripts": str(image_paths["bin"]),
    })
    original_packaging_errors = checker.packaging_errors
    monkeypatch.setattr(checker, "packaging_errors", lambda *args: (
        original_packaging_errors(*args, find_spec=lambda _name: None)
    ))
    monkeypatch.setattr(checker.shutil, "which", lambda _name: None)
    monkeypatch.setattr(checker, "debian_version_errors", lambda: [])


def test_main_reports_runtime_import_failure_and_returns_nonzero(
    monkeypatch, tmp_path, image_paths, capsys,
):
    _isolated_main(monkeypatch, tmp_path, image_paths)
    monkeypatch.setattr(checker, "RUNTIME_IMPORTS", ("working", "broken"))

    def import_runtime(module):
        if module == "broken":
            raise ImportError("missing shared library")
        return object()

    monkeypatch.setattr(checker.importlib, "import_module", import_runtime)

    assert checker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["runtime_imports"] == ["working"]
    assert result["errors"] == ["runtime import broken: ImportError"]


def test_main_stops_before_runtime_imports_when_tools_remain(
    monkeypatch, tmp_path, image_paths, capsys,
):
    _isolated_main(monkeypatch, tmp_path, image_paths)
    (image_paths["bin"] / "pip3.11").touch()

    def unexpected_import(module):
        pytest.fail(f"runtime import must wait for image validation: {module}")

    monkeypatch.setattr(checker.importlib, "import_module", unexpected_import)

    assert checker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["runtime_imports"] == []
    assert "packaging executable remains: pip3.11" in result["errors"]


def test_main_reports_debian_version_failure_before_runtime_imports(
    monkeypatch, tmp_path, image_paths, capsys,
):
    _isolated_main(monkeypatch, tmp_path, image_paths)
    monkeypatch.setattr(checker, "debian_version_errors", lambda: [
        "Debian package missing: gzip",
    ])

    def unexpected_import(module):
        pytest.fail(f"runtime import must wait for Debian validation: {module}")

    monkeypatch.setattr(checker.importlib, "import_module", unexpected_import)

    assert checker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["runtime_imports"] == []
    assert result["errors"] == ["Debian package missing: gzip"]


def test_docker_installs_binary_dependencies_before_checking_and_removing_tools():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = dockerfile.replace("\\\n", " ").splitlines()
    install = next(line for line in instructions if line.startswith("RUN ") and "-r requirements-prod.txt" in line)
    commands = [shlex.split(part.strip()) for part in install[4:].split("&&")]
    installs = [command for command in commands if command[:4] == ["python", "-m", "pip", "install"]]

    assert installs
    assert all("--only-binary=:all:" in command for command in installs)
    assert all(not any(option.startswith("--no-binary") for option in command) for command in installs)
    check = commands.index(["python", "-m", "pip", "check"])
    uninstall = next(index for index, command in enumerate(commands) if command[:4] == ["python", "-m", "pip", "uninstall"])
    assert max(commands.index(command) for command in installs) < check < uninstall
    assert {"pip", "setuptools", "wheel"} <= set(commands[uninstall][4:])
    cleanup = " ".join(" ".join(command) for command in commands[uninstall + 1:])
    assert "rmtree" in cleanup and "ensurepip" in cleanup and "stdlib" in cleanup


def test_ci_runs_checker_inside_the_actual_built_image_from_stdin():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(
        step for step in workflow["jobs"]["test"]["steps"]
        if "scripts/check_runtime_image.py" in step.get("run", "")
    )
    lines = step["run"].replace("\\\n", " ").splitlines()
    build = shlex.split(next(line for line in lines if line.lstrip().startswith("docker build ")))
    run = shlex.split(next(line for line in lines if "< scripts/check_runtime_image.py" in line))
    image = build[build.index("--tag") + 1]

    assert run[:2] == ["docker", "run"]
    assert run[run.index("--entrypoint") + 1] == "python"
    assert run.index(image) < run.index("-") < run.index("<")
    assert run[run.index("<") + 1] == "scripts/check_runtime_image.py"
    assert run[run.index("--network") + 1] == "none"
    assert "--read-only" in run and "-i" in run
