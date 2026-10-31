"""Actual sealed files/config/constructors; isolated CLI assembly, no Docker/PG."""
from contextlib import contextmanager
from copy import deepcopy
import os
import sys
from types import SimpleNamespace

import pytest

from production_admission_environment_support import sealed_inputs
from test_sender_drain_contract import synthetic_manifest
from core import config
from scripts import controlled_release_finalize as final
from scripts import production_admission_supervisor as supervisor
from services import admission_fence as gate
from durable_handoff_cutover import CutoverError


@pytest.fixture
def cli_case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    with sealed_inputs(root / "sealed-inputs") as (fixture, permit):
        monkeypatch.setattr(supervisor, "ROOT", fixture["root"])
        migration = root / "migration"
        migration.mkdir(mode=0o700)
        gate._write(migration / "permit.json", permit)
        original, _body = synthetic_manifest(root)
        binding = original["sender_binding"]
        binding["source_sha256"] = final.sender.bridge_source()
        binding["application_source_sha256"] = final._sha(
            (final.ROOT / "ops/miniflux/pause-source-lock.json").read_bytes())
        identity = original["postgres_identity"]
        identity["schema_version"] = 135
        sender_plan = {"schema": 1, "scope": "instrumented_sender_only", "binding": binding,
                       "postgres_identity": identity, "window": original["window"], "sources": final.sender.sources()}
        for name, value in (("sender.json", sender_plan), ("token.json", {"token": "a" * 64}),
                            ("dsn.json", {"dsn": "postgresql://synthetic@127.0.0.1/synthetic_outbox"})):
            gate._write(root / name, value)
        request = {"plan": {"image_ref": "synthetic:finalize"},
                   "receiver": {"migration_root": str(migration), "runtime_root": str(root / "runtime"),
                                "state": str(root / "state"), "operation_id": "b" * 64},
                   "sender": {"plan_file": str(root / "sender.json"), "token_file": str(root / "token.json"),
                              "dsn_file": str(root / "dsn.json"), "url": "http://127.0.0.1:8081",
                              "evidence_root": str(root / "sender-evidence")}, "evidence_root": str(root / "finalize")}
        gate._write(root / "request.json", request)
        yield SimpleNamespace(root=root, fixture=fixture, permit=permit, sender_plan=sender_plan, request=request, calls=[])


def assemble(case, monkeypatch, action, *, fail_backend=False):
    monkeypatch.setattr(sys, "argv", [str(final.__file__), "--action", action, "--image-ref", "synthetic:finalize",
                                     "--request", str(case.root / "request.json")])
    session = object()

    @contextmanager
    def locked(*args, **kwargs):
        assert args == tuple(case.request["receiver"][key] for key in ("migration_root", "runtime_root", "state"))
        assert kwargs == {"operation_id": case.request["receiver"]["operation_id"]}
        case.calls.append("lock")
        yield session

    def backend(receiver, driver, plan, evidence_root):
        assert receiver is session and plan == case.request["plan"]
        assert evidence_root == case.request["evidence_root"]
        assert type(driver) is final.sender.SenderActivationDriver
        assert type(driver.transport) is final.sender.NativeSenderActivationTransport
        assert type(driver.postgres) is final.sender.PostgresReader
        assert driver.plan == case.sender_plan and driver.postgres.expected == case.sender_plan["postgres_identity"]
        assert driver.transport.container_id == case.sender_plan["binding"]["component"]["id"]
        assert gate._hash(config.config.cfg) == case.permit["config"]["effective_sha256"]
        assert config.config.cfg["security"]["api_key"] == "synthetic-child-secret-0123456789"
        assert os.environ["AI_FILTER_CONFIG_PATH"] == case.permit["config"]["path"]
        assert os.environ["AI_FILTER_API_KEY"] == "synthetic-child-secret-0123456789"
        assert "AMBIENT_ONLY_SENTINEL" not in os.environ
        assert os.environ["RELEASE_HEALTH_TIMEOUT_SECONDS"] == "15"
        assert os.environ["RELEASE_HEALTH_POLL_SECONDS"] == "1"
        assert os.environ["RELEASE_TEST_ATTESTATION_DIR"] == str(case.root / "attestations")
        case.calls.append(action)
        if fail_backend:
            raise RuntimeError("synthetic backend failure")
        return {"action": action, "assembly_only": True}

    monkeypatch.setattr(final.activation, "locked_activation", locked)
    monkeypatch.setattr(final, action, backend)
    monkeypatch.setenv("AI_FILTER_API_KEY", "ambient-wrong-key")
    monkeypatch.setenv("AI_FILTER_CONFIG_PATH", str(case.root / "ambient-missing.yaml"))
    monkeypatch.setenv("AMBIENT_ONLY_SENTINEL", "removed")
    monkeypatch.setenv("RELEASE_HEALTH_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("RELEASE_HEALTH_POLL_SECONDS", "1")
    monkeypatch.setenv("RELEASE_TEST_ATTESTATION_DIR", str(case.root / "attestations"))


@pytest.mark.parametrize("action", ["finalize", "query"])
def test_cli_uses_actual_sealed_configuration_and_exact_sender_namespace(cli_case, monkeypatch, capsys, action):
    assemble(cli_case, monkeypatch, action)
    previous_env, previous_cfg = dict(os.environ), config.config.cfg
    final.main()
    assert cli_case.calls == ["lock", action]
    assert dict(os.environ) == previous_env and config.config.cfg is previous_cfg
    output = capsys.readouterr().out
    assert '"assembly_only": true' in output and "secret" not in output and "ambient-wrong-key" not in output


def test_cli_seals_configuration_before_receiver_lock(cli_case, monkeypatch):
    assemble(cli_case, monkeypatch, "finalize")

    @contextmanager
    def locked(*_args, **_kwargs):
        gate._require(gate._hash(config.config.cfg) == cli_case.permit["config"]["effective_sha256"],
                      "sealed configuration missing before receiver lock")
        raise RuntimeError("synthetic boundary reached")
        yield  # pragma: no cover - context manager protocol; boundary deliberately raises

    monkeypatch.setattr(final.activation, "locked_activation", locked)
    with pytest.raises(RuntimeError, match="synthetic boundary reached"):
        final.main()
    assert not cli_case.calls


@pytest.mark.parametrize("action", ["finalize", "query"])
@pytest.mark.parametrize("mutation", ["effective_config", "original_config", "sealed_environment", "sender_cid",
                                    "sender_cid_none", "sender_token"])
def test_cli_rejects_drift_before_capability_or_backend(cli_case, monkeypatch, action, mutation):
    assemble(cli_case, monkeypatch, action)
    if mutation == "effective_config":
        permit = deepcopy(cli_case.permit)
        permit["config"]["effective_sha256"] = "0" * 64
        gate._write(cli_case.root / "migration/permit.json", permit)
    elif mutation == "original_config":
        cli_case.fixture["config"].write_bytes(b"threshold: {pass: 8}\n")
    elif mutation == "sealed_environment":
        path = cli_case.fixture["output"] / "environment"
        path.chmod(0o600)
        path.write_bytes(b"AI_FILTER_API_KEY=changed\n")
        path.chmod(0o400)
    elif mutation in {"sender_cid", "sender_cid_none"}:
        plan = deepcopy(cli_case.sender_plan)
        plan["binding"]["component"]["id"] = "short" if mutation == "sender_cid" else None
        gate._write(cli_case.root / "sender.json", plan)
    else:
        gate._write(cli_case.root / "token.json", {"token": "short"})
    previous_env, previous_cfg = dict(os.environ), config.config.cfg
    with pytest.raises((gate.AdmissionError, final.private.ContractError, CutoverError)):
        final.main()
    assert not cli_case.calls
    assert not (cli_case.root / "sender-evidence").exists()
    assert dict(os.environ) == previous_env and config.config.cfg is previous_cfg


def test_cli_failure_restores_configuration_without_retry(cli_case, monkeypatch):
    assemble(cli_case, monkeypatch, "finalize", fail_backend=True)
    previous_env, previous_cfg = dict(os.environ), config.config.cfg
    with pytest.raises(RuntimeError, match="synthetic backend failure"):
        final.main()
    assert cli_case.calls == ["lock", "finalize"]
    assert dict(os.environ) == previous_env and config.config.cfg is previous_cfg
