"""Offline fixed-source packaging guard; actual Go/PG tests live in the patch."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("outbox_patch", ROOT / "scripts/apply_miniflux_outbox.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_lock_and_patch_identity():
    lock = json.loads(MODULE.LOCK.read_text())
    patch = MODULE.LOCK.parent / lock["patch"]
    assert lock["revision"] == "51f2e0d8199ea8fa305081f6e175bba64b0ef94b"
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == lock["patch_sha256"]
    assert lock["activation_default"] is False
    assert lock["build_status"] == "source_lock_only_not_a_build_or_deployment_receipt_see_separate_evidence"


def fake_git(monkeypatch, *, revision=None, dirty=False, patch_error=False):
    calls = []
    lock = json.loads(MODULE.LOCK.read_text())

    def run(source, *args):
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return revision or lock["revision"]
        if args[0] == "status":
            return " M tracked.go" if dirty else ""
        if patch_error and args[:2] == ("apply", "--check"):
            raise ValueError("patch does not apply")
        return ""

    monkeypatch.setattr(MODULE, "git", run)
    monkeypatch.setattr(MODULE, "archive_hash", lambda _: lock["base_git_archive_tar_sha256"])
    monkeypatch.setattr(MODULE, "verify_patched_files", lambda *args: None)
    return calls


def test_dry_run_never_applies(monkeypatch, tmp_path):
    calls = fake_git(monkeypatch)
    assert MODULE.apply(tmp_path)["status"] == "validated_only"
    assert not any(c[0] == "apply" and c[1] != "--check" for c in calls)


def test_explicit_apply_checks_first(monkeypatch, tmp_path):
    calls = fake_git(monkeypatch)
    assert MODULE.apply(tmp_path, True)["status"] == "applied"
    assert calls[-3][0:2] == ("apply", "--check")
    assert calls[-2][0] == "apply"
    assert calls[-1] == ("diff", "--check")


@pytest.mark.parametrize("kwargs", [{"revision": "a" * 40}, {"dirty": True}, {"patch_error": True}])
def test_wrong_revision_dirty_or_failed_check_blocks_apply(monkeypatch, tmp_path, kwargs):
    calls = fake_git(monkeypatch, **kwargs)
    with pytest.raises(ValueError):
        MODULE.apply(tmp_path, True)
    assert not any(c[0] == "apply" and c[1] != "--check" for c in calls)


def test_tampered_patch_blocks_git_access(tmp_path, monkeypatch):
    lock = json.loads(MODULE.LOCK.read_text())
    (tmp_path / lock["patch"]).write_text("tampered")
    (tmp_path / "source-lock.json").write_text(json.dumps(lock))
    monkeypatch.setattr(MODULE, "git", lambda *args: pytest.fail("git reached before hash validation"))
    with pytest.raises(ValueError, match="hash"):
        MODULE.verify(tmp_path, tmp_path / "source-lock.json")


def test_upstream_patch_retains_explicit_safety_contracts():
    patch = (ROOT / "ops/miniflux/0001-durable-webhook-outbox.patch").read_text()
    for text in ["SET LOCAL synchronous_commit=on", "pg_try_advisory_xact_lock", "FOR UPDATE SKIP LOCKED", "boundedEntryTransaction"]:
        assert text in patch
    assert "TestPostgresOutboxAtomicRejectAndDuplicate" in patch
    assert "TestPostgresOutboxRestartVerify" in patch
    assert "TestDurableRejectsOrdinaryStatusAndWrongReceipts" in patch
    assert "TestDurableRedirectDoesNotLeakFrozenBodyOrSignature" in patch
    assert "TestDurableReservedKeyCaseAliasesNeverAcknowledge" in patch
    assert "TestPostgresOutboxEntrySQLSharesThreeSecondDeadline" in patch
    assert "TestPostgresOutboxClaimsForceSynchronousCommit" in patch


def test_wrong_archive_blocks_apply(monkeypatch, tmp_path):
    calls = fake_git(monkeypatch)
    monkeypatch.setattr(MODULE, "archive_hash", lambda _: "wrong")
    with pytest.raises(ValueError, match="archive"):
        MODULE.apply(tmp_path, True)
    assert not any(c[0] == "apply" for c in calls)


def test_patched_file_hashes_fail_closed(tmp_path):
    path = tmp_path / "fixed.go"
    path.write_bytes(b"reviewed\n")
    expected = {"fixed.go": hashlib.sha256(path.read_bytes()).hexdigest()}
    MODULE.verify_patched_files(tmp_path, expected)
    path.write_bytes(b"shifted\n")
    with pytest.raises(ValueError, match="differs"):
        MODULE.verify_patched_files(tmp_path, expected)
    with pytest.raises(ValueError, match="escapes"):
        MODULE.verify_patched_files(tmp_path, {"../outside": "x"})


def test_actions_service_healthcheck_uses_runner_argument_quotes():
    # Actions service options are process arguments, not a shell script. The
    # first CI run passed literal single quotes and exposed -U to Docker.
    workflow = (ROOT / ".github/workflows/miniflux-outbox.yml").read_text()
    assert '--health-cmd "pg_isready -U postgres -d synthetic_outbox"' in workflow
    assert "--health-cmd '" not in workflow
