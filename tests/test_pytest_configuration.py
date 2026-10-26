"""Exercise the real config in a tiny project, without the application fixtures."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_probe(root, *arguments):
    return subprocess.run(
        [sys.executable, "-I", "-m", "pytest", *arguments],
        cwd=root,
        env={"PATH": os.defpath, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_default_config_discovers_tests_imports_root_and_shows_deprecations(tmp_path):
    shutil.copy2(PROJECT_ROOT / "pytest.ini", tmp_path / "pytest.ini")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "project_probe.py").write_text(
        "import warnings\n"
        "def emit():\n"
        "    warnings.warn('project deprecation remains visible', DeprecationWarning)\n",
        encoding="utf-8",
    )
    (tmp_path / "vendor_probe.py").write_text(
        "import warnings\n"
        "def emit():\n"
        "    warnings.warn('vendor deprecation remains visible', DeprecationWarning)\n",
        encoding="utf-8",
    )
    (tests / "test_warning_visibility.py").write_text(
        "import project_probe\nimport vendor_probe\n"
        "def test_deprecations_do_not_fail():\n"
        "    project_probe.emit()\n    vendor_probe.emit()\n",
        encoding="utf-8",
    )
    (tmp_path / "test_outside_configured_path.py").write_text(
        "def test_not_collected():\n    raise AssertionError('outside tests directory')\n",
        encoding="utf-8",
    )

    result = _run_probe(tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "project deprecation remains visible" in output
    assert "vendor deprecation remains visible" in output
    assert "1 passed" in output
    assert "test session starts" not in output
    assert "outside tests directory" not in output

    # The same inputs demonstrate why the former global ignore was too broad.
    hidden = _run_probe(tmp_path, "-o", "filterwarnings=ignore::DeprecationWarning")
    hidden_output = hidden.stdout + hidden.stderr
    assert hidden.returncode == 0, hidden_output
    assert "project deprecation remains visible" not in hidden_output
    assert "vendor deprecation remains visible" not in hidden_output
