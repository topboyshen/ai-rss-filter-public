import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "scripts/miniflux_dump_selection.py"
SPEC = importlib.util.spec_from_file_location("miniflux_dump_selection", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DumpSelectionTests(unittest.TestCase):
    def fixture(self, root):
        values = {"PLAN.json": {"production": False, "models": 0,
                     "names": {"SOURCE": "held_source_isolation_" + "a" * 16}},
                  "SYNTHETIC_DUMP.json": {"exit_code": 0, "large_byte_fixture_raw_not_exported": True,
                     "sql": "synthetic small fixtures", "sha256": hashlib.sha256(b"synthetic small fixtures").hexdigest()}}
        row = {"delivery_id": "b" * 64, "body_sha256": "b" * 64,
               "state": "held", "body_bytes": 1048576, "charged_bytes": 1049600}
        for table in ("webhook_outbox", "webhook_legacy_originals"):
            values["BYTE_FIXTURE_" + table + ".json"] = [copy.deepcopy(row)]
        for name, value in values.items():
            (root / name).write_text(json.dumps(value))
        return values

    def test_no_legacy_verifier_means_original_full_dump(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(MODULE.dump_arguments(Path(folder)), [])

    def test_only_exact_current_byte_fixture_data_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            self.assertEqual(MODULE.dump_arguments(root), [
                "--exclude-table-data=held_source_isolation_" + "a" * 16 + "_bytes." + table
                for table in ("webhook_outbox", "webhook_legacy_originals")])

    def test_missing_export_or_either_summary_fails_not_silent_exclusion(self):
        for name in ("SYNTHETIC_DUMP.json", "BYTE_FIXTURE_webhook_outbox.json", "BYTE_FIXTURE_webhook_legacy_originals.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.fixture(root)
                (root / name).unlink()
                with self.assertRaises(FileNotFoundError):
                    MODULE.dump_arguments(root)

    def test_invalid_scope_export_or_row_never_widens_exclusion(self):
        mutations = [("PLAN.json", lambda v: v.update(production=True)),
                     ("PLAN.json", lambda v: v["names"].update(SOURCE="*")),
                     ("SYNTHETIC_DUMP.json", lambda v: v.update(exit_code=7)),
                     ("SYNTHETIC_DUMP.json", lambda v: v.update(sha256="a" * 64)),
                     ("BYTE_FIXTURE_webhook_outbox.json", lambda v: v[0].update(body_sha256="c" * 64)),
                     ("BYTE_FIXTURE_webhook_outbox.json", lambda v: v[0].update(body_bytes=True)),
                     ("BYTE_FIXTURE_webhook_legacy_originals.json", lambda v: v.clear())]
        for name, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                values = self.fixture(root)
                mutate(values[name])
                (root / name).write_text(json.dumps(values[name]))
                with self.assertRaises(ValueError):
                    MODULE.dump_arguments(root)


if __name__ == "__main__":
    unittest.main()
