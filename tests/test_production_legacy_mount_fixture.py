"""The actual activation harness must start from one old data bind."""
from pathlib import Path

from scripts import production_activation_fixture as fixture
from scripts.private_control_subpaths import capture
from services import admission_fence as gate


def test_actual_native_fixture_keeps_old_control_mounts_absent(tmp_path, monkeypatch):
    from core import config
    monkeypatch.setattr(config, "CONFIG_PATH", config.CONFIG_PATH)
    monkeypatch.setattr(config.config, "_cfg", config.config._cfg)
    monkeypatch.setattr(config.config, "_blacklist_re", config.config._blacklist_re)
    monkeypatch.setenv("AI_FILTER_API_KEY", "synthetic-previous")
    monkeypatch.setattr(gate, "_boot_identity", lambda: "synthetic-unit-boot")
    p = fixture.prepare_disk(tmp_path / "synthetic", {"source_sha": "a" * 40}, "internal", "sha256:" + "b" * 64)
    try:
        data = p["paths"]["data"]
        previous, names = fixture.previous_manifest(p, "python@sha256:" + "c" * 64)
        assert set(previous["services"]) == set(names.values())
        for service in previous["services"].values():
            writable = [m for m in service["volumes"] if not m.get("read_only", False)]
            assert writable == [{"type": "bind", "source": str(data), "target": "/app/data",
                                  "read_only": False, "bind": {"create_host_path": False}}]
            assert len(service["volumes"]) == 2  # data and immutable config only
            assert not any(key.startswith(("AI_FILTER_PRODUCTION_STARTUP_", "AI_FILTER_ADMISSION_"))
                           for key in service["environment"])
        for kind, name in (("fence", "admission_fence"), ("slots", "startup_slots"),
                           ("spool", "webhook_spool"), ("journal", "admission_journal")):
            assert p["paths"][kind] == data / name
        for service in p["manifest"]["services"].values():
            assert service["environment"]["AI_FILTER_DB_PATH"] == "/app/data/fixture.db"
            assert [m for m in service["volumes"] if not m.get("read_only", False)] == writable
        assert p["database"] == data / "fixture.db"
        assert p["database"].is_file()
        assert not p["paths"]["grants"].is_relative_to(data)
        for kind in ("fence", "slots"):
            source = p["paths"][kind]
            value = capture(source, str(Path("/app/data") / source.relative_to(data)), data)
            assert value["directories"][-1]["inode"] == source.stat().st_ino
        marker = gate._read(p["directory"] / "SYNTHETIC_EMPTY_RECEIPT_STORE.json")
        assert marker["accepted_originals"] == 0 and marker["production_missing_metadata_authorized"] is False
    finally:
        p["anchor"].close()
