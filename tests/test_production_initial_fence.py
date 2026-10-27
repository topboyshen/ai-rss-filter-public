from copy import deepcopy
import time

import pytest

from services import admission_fence as gate, production_initial_fence as initial


@pytest.fixture
def pristine(tmp_path, monkeypatch):
    # Unit test only; the native fixture uses the actual host boot identity.
    monkeypatch.setattr(gate, "_boot_identity", lambda: "b" * 64)
    root = tmp_path.resolve() / "fence"
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64,
               "strategy": "synthetic", "source_sha256": gate.source_sha256(), "deployment_nonce": "c" * 32}
    now = int(time.time())
    gate.provision(root, binding, {"not_before": now - 1, "expires_at": now + 300,
                                 "max_admissions": 5, "max_bytes": 1000})
    return root, binding


def test_initial_closed_never_opens_and_original_query_is_exact(pristine):
    root, binding = pristine
    before = (root / "state.json").read_bytes()
    result = initial.confirm(root, binding, operation_id="1" * 64)
    assert result["generation"] == 0 and result["phase"] == "frozen"
    assert result["historical_producers_verified"] is False and not result["business_open"]
    assert (root / "state.json").read_bytes() == before
    assert initial.query(root, binding, operation_id="1" * 64) == result
    assert not list((root / "operations").iterdir())
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        initial.confirm(root, binding, operation_id="1" * 64)
    with pytest.raises(gate.AdmissionError, match="changed"):
        initial.query(root, binding, operation_id="2" * 64)


def test_previous_open_stop_freeze_is_not_initial(pristine):
    root, binding = pristine
    fence = gate.Fence(root, binding)
    fence.open_trial("a" * 64, 0, "synthetic")
    fence.stop_new("b" * 64, 1, "synthetic")
    fence.freeze("c" * 64, 2, "synthetic")
    with pytest.raises(gate.AdmissionError, match="never-opened"):
        initial.confirm(root, binding, operation_id="d" * 64)


def test_confirmation_ack_loss_queries_original_without_reopening(pristine, monkeypatch):
    root, binding = pristine
    write = gate._write
    def fail_final(path, value):
        if path.name == "initial_closed.json":
            raise OSError("synthetic final sync failure")
        return write(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(gate, "_write", fail_final)
        with pytest.raises(OSError):
            initial.confirm(root, binding, operation_id="a" * 64)
    assert (root / "startup/initial_closed.intent.json").exists()
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        initial.confirm(root, binding, operation_id="b" * 64)
    result = initial.query(root, binding, operation_id="a" * 64)
    assert result["generation"] == 0


@pytest.mark.parametrize("field", ["operation_id", "binding", "manifest_sha256", "receipt_sha256", "generation"])
def test_initial_runtime_identity_is_exact(pristine, field):
    root, binding = pristine
    result = initial.confirm(root, binding, operation_id="a" * 64)
    record = {**result, "receipt_sha256": gate._hash(result)}
    changed = deepcopy(record)
    if field == "binding":
        changed[field]["deployment_nonce"] = "d" * 32
    else:
        changed[field] = 1 if field == "generation" else "b" * 64
    with pytest.raises(gate.AdmissionError, match="differs"):
        initial.read_receipt(root, changed)


@pytest.mark.parametrize("name", ["initial_closed.intent.json", "initial_closed.json"])
def test_pending_only_is_not_authority_until_exact_query_confirms(pristine, name):
    root, binding = pristine
    value = initial.confirm(root, binding, operation_id="a" * 64)
    record = {**value, "receipt_sha256": gate._hash(value)}
    path = root / "startup" / name
    path.rename(path.with_name(name + ".pending"))
    with pytest.raises(gate.AdmissionError, match="unconfirmed"):
        initial.read_receipt(root, record)
    assert initial.query(root, binding, operation_id="a" * 64) == value
    assert initial.read_receipt(root, record) == value
