"""Real sealed files, clean synthetic Git source and an isolated Python child."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from production_admission_environment_support import sealed_inputs
from scripts import production_admission_supervisor as supervisor
from scripts import production_admission_watchdog as watchdog
from services import admission_fence as gate, production_admission_lifecycle as life


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    with sealed_inputs(tmp_path.resolve() / "environment-case") as (files, permit):
        monkeypatch.setattr(supervisor, "ROOT", files["root"])
        permit["freeze"] = {"synthetic": True}
        migration = tmp_path.resolve() / "migration"
        migration.mkdir(mode=0o700)
        gate._write(migration / "permit.json", permit)
        plan = {"migration_root": str(migration), "config_path": permit["config"]["path"],
                "freeze": permit["freeze"], "source_sha256": life.source_sha256()}
        yield files, permit, plan


def test_sealed_environment_drops_ambient_injections_and_restores_parent_configuration(sealed, monkeypatch):
    from core import config
    files, permit, plan = sealed
    monkeypatch.setenv("AI_FILTER_API_KEY", "wrong-ambient-synthetic")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/unsafe")
    monkeypatch.setenv("LD_PRELOAD", "/synthetic/unsafe.so")
    before_env, before_cfg, before_path = dict(os.environ), config.config.cfg, config.CONFIG_PATH
    expected = supervisor.sealed_child_environment(plan)
    assert "LD_PRELOAD" not in expected and "PYTHONPATH" not in expected
    assert expected["AI_FILTER_API_KEY"] == "synthetic-child-secret-0123456789"
    assert dict(os.environ) == before_env
    with supervisor.sealed_configuration(permit, source_root=files["root"]) as actual:
        assert dict(os.environ) == expected
        assert actual["security"]["api_key"] == "synthetic-child-secret-0123456789"
        assert gate._hash(actual) == permit["config"]["effective_sha256"]
    assert dict(os.environ) == before_env and config.config.cfg is before_cfg and config.CONFIG_PATH == before_path


def test_actual_isolated_child_sees_same_effective_config_and_old_minimal_environment_does_not(sealed):
    files, permit, plan = sealed
    repository = Path(watchdog.__file__).resolve().parents[1]
    code = ("import json,sys; from pathlib import Path; "
            f"sys.path[:0]=[{str(repository)!r},{str(repository / 'scripts')!r}]; "
            "from scripts import production_admission_supervisor as s; "
            f"s.ROOT=Path({str(files['root'])!r}); plan=json.loads(sys.argv[1]); "
            "sys.platform.startswith('linux') and s.verify_child_environment(plan); from core.config import config; "
            "from services.admission_fence import _hash; print(_hash(config.cfg))")
    expected = supervisor.sealed_child_environment(plan)
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, json.dumps(plan)],
        env=expected, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == permit["config"]["effective_sha256"]
    # Reproduce the actual old launch: only config path plus fixed PATH/LANG.
    old = {key: expected[key] for key in ("PATH", "LANG", "AI_FILTER_CONFIG_PATH")}
    old_code = ("import sys; " + f"sys.path.insert(0,{str(repository)!r}); "
                "from core.config import config; from services.admission_fence import _hash; print(_hash(config.cfg))")
    missing = subprocess.run([sys.executable, "-I", "-B", "-c", old_code], env=old,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert missing.returncode == 0 and missing.stdout.strip() != permit["config"]["effective_sha256"]
    assert b"synthetic-child-secret" not in result.stdout.encode() + result.stderr.encode()


def test_exact_exec_environment_reloads_actual_configuration(sealed, monkeypatch):
    from core import config
    _files, permit, plan = sealed
    # Darwin exec injects __CF_USER_TEXT_ENCODING; do not allow that key in the
    # Linux production contract. The actual child above checks full env on Linux.
    monkeypatch.setattr(os, "environ", supervisor.sealed_child_environment(plan))
    monkeypatch.setattr(config, "CONFIG_PATH", config.CONFIG_PATH)
    monkeypatch.setattr(config.config, "_cfg", config.config._cfg)
    monkeypatch.setattr(config.config, "_blacklist_re", config.config._blacklist_re)
    supervisor.verify_child_environment(plan)
    assert gate._hash(config.config.cfg) == permit["config"]["effective_sha256"]


@pytest.mark.parametrize("change", ["extra", "wrong_secret", "missing_secret", "config_path"])
def test_exec_validation_rejects_nonsealed_environment(sealed, monkeypatch, change):
    _files, _permit, plan = sealed
    expected = supervisor.sealed_child_environment(plan)
    monkeypatch.setattr(os, "environ", deepcopy(expected))
    if change == "extra":
        os.environ["UNAPPROVED_AMBIENT"] = "ignored-is-not-allowed"
    elif change == "wrong_secret":
        os.environ["AI_FILTER_API_KEY"] = "wrong"
    elif change == "missing_secret":
        os.environ.pop("AI_FILTER_API_KEY")
    else:
        os.environ["AI_FILTER_CONFIG_PATH"] = "/synthetic/other"
    with pytest.raises(gate.AdmissionError, match="child environment differs"):
        supervisor.verify_child_environment(plan)


@pytest.mark.parametrize("field", ["environment", "source", "manifest", "config", "effective"])
def test_bound_file_or_identity_change_is_not_overridden_by_ambient(sealed, monkeypatch, field):
    files, permit, plan = sealed
    if field == "environment":
        files["environment"].write_bytes(b"AI_FILTER_API_KEY=changed\n")
    elif field == "source":
        permit["source"]["revision"] = "0" * 40
    elif field == "manifest":
        permit["source"]["manifest_sha256"] = "0" * 64
    elif field == "config":
        permit["config"]["sha256"] = "0" * 64
    else:
        permit["config"]["effective_sha256"] = "0" * 64
    gate._write(Path(plan["migration_root"]) / "permit.json", permit)
    before = dict(os.environ)
    with pytest.raises((gate.AdmissionError, supervisor.private.ContractError)):
        with supervisor.sealed_configuration(permit, source_root=files["root"]):
            raise AssertionError("changed configuration must not execute")
    assert dict(os.environ) == before


@pytest.mark.parametrize("key", ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "PATH", "HOME", "DOCKER_HOST"])
def test_private_environment_rejects_host_execution_overrides(key):
    with pytest.raises(supervisor.private.ContractError, match="plain NAME=value"):
        supervisor.private.environment_values((key + "=synthetic\n").encode())


@pytest.mark.parametrize("module", [supervisor, watchdog])
def test_both_launches_use_real_sealed_environment(sealed, monkeypatch, module, tmp_path):
    _files, _permit, plan = sealed
    calls = []
    root = tmp_path.resolve() / "launch"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(module, "load", lambda _path: (plan, root, {}) if module is supervisor else (plan, root))
    original = module.subprocess.Popen
    monkeypatch.setattr(module.subprocess, "Popen", lambda argv, **kwargs:
                        (calls.append((argv, kwargs)) or type("Child", (), {"pid": 123})())
                        if argv[0] == sys.executable else original(argv, **kwargs))
    monkeypatch.setenv("AI_FILTER_API_KEY", "wrong-ambient")
    module.start(root / "plan.json")
    assert calls[0][1]["env"] == supervisor.sealed_child_environment(plan)
    assert calls[0][1]["close_fds"] and calls[0][0][1:3] == ["-I", "-B"]
