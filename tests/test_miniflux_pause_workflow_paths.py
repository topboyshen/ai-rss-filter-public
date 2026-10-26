"""Check actual workflow path selection without Docker, PostgreSQL or HTTP."""

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ".github/workflows/miniflux-pause.yml"


def matches_path(path, pattern):
    """Match the literal paths, * and ** used by this workflow.

    Unlike fnmatch, a single * cannot cross a directory separator. Reject
    unsupported syntax so a future pattern change needs an explicit review.
    """
    if any(character in pattern for character in "!?+[]{}"):
        raise ValueError("workflow path syntax needs a matching regression")
    expression = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(expression, path) is not None


class MinifluxPauseWorkflowPathsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves the GitHub Actions key `on` as a string.
        cls.events = yaml.load(
            (ROOT / WORKFLOW_PATH).read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )["on"]

    def selected(self, event, changed):
        return any(
            matches_path(path, pattern)
            for path in changed
            for pattern in self.events[event]["paths"]
        )

    def test_actual_pg_dependencies_trigger_individually_for_both_events(self):
        paths = (
            "scripts/observe_handoff_capacity.py",
            "scripts/independent_drain_inventory.py",
            "scripts/durable_handoff_cutover.py",
            "scripts/apply_miniflux_outbox.py",
            "services/webhook_receipts.py",
            "services/natural_score_continuations.py",
            "tests/test_miniflux_legacy_access.py",
            "tests/test_handoff_capacity_observer.py",
            "tests/test_independent_drain_inventory.py",
        )
        for event in ("pull_request", "push"):
            for path in paths:
                with self.subTest(event=event, path=path):
                    self.assertTrue(self.selected(event, [path]))

    def test_existing_source_harness_and_self_changes_still_trigger(self):
        paths = (
            "ops/miniflux/0002-unified-pause-and-held-originals.patch",
            "ops/miniflux/held-access/verify_legacy.py",
            "ops/miniflux/held-access/legacy_audit.sql",
            "ops/miniflux/held-access/nested/fixture.sql",
            "ops/miniflux/pause-source-lock.json",
            "scripts/apply_miniflux_pause.py",
            "scripts/verify_miniflux_pause_native.py",
            "scripts/miniflux_pause_fixture.py",
            "scripts/miniflux_handoff_adapter.py",
            "scripts/miniflux_host_joint_native.py",
            "scripts/miniflux_dump_selection.py",
            "scripts/handoff_host_cron.py",
            "scripts/verify_handoff_operations_native.py",
            "tests/test_miniflux_pause_workflow_paths.py",
            "tests/test_miniflux_handoff_adapter.py",
            "tests/test_miniflux_host_joint_native.py",
            "tests/test_miniflux_dump_selection.py",
            WORKFLOW_PATH,
        )
        for event in ("pull_request", "push"):
            for path in paths:
                with self.subTest(event=event, path=path):
                    self.assertTrue(self.selected(event, [path]))

    def test_unrelated_or_similarly_named_files_do_not_trigger(self):
        paths = (
            "README.md", "docs/miniflux-unified-pause-review.md",
            "core/scorer.py", "config.yaml", ".github/workflows/ci.yml",
            "services/unrelated.py", "scripts/unrelated.py",
            "ops/other/fixture.sql", "scripts/nested/miniflux_pause.py",
            "tests/test_unrelated.py", "tests/test_ci_change_scope.py",
            "scripts/observe_handoff_capacity.py.md",
            "scripts/nested/observe_handoff_capacity.py",
            "tests/test_miniflux_legacy_access.py.bak",
            "tests/nested/test_miniflux_pause_workflow_paths.py",
        )
        for event in ("pull_request", "push"):
            for path in paths:
                with self.subTest(event=event, path=path):
                    self.assertFalse(self.selected(event, [path]))
            self.assertFalse(self.selected(event, paths))
            self.assertFalse(self.selected(event, []))
            self.assertTrue(self.selected(event, [*paths, "scripts/observe_handoff_capacity.py"]))

    def test_master_push_and_stacked_pull_requests_keep_their_branch_scope(self):
        self.assertEqual(self.events["push"]["branches"], ["master"])
        for key in ("branches", "branches-ignore", "paths-ignore"):
            self.assertNotIn(key, self.events["pull_request"])
        for key in ("branches-ignore", "tags", "tags-ignore", "paths-ignore"):
            self.assertNotIn(key, self.events["push"])

    def test_matcher_distinguishes_single_star_from_recursive_paths(self):
        self.assertTrue(matches_path("ops/miniflux/nested/verify.py", "ops/miniflux/**"))
        self.assertTrue(matches_path("tests/test_miniflux_pause_new.py", "tests/test_miniflux_pause*.py"))
        self.assertFalse(matches_path("tests/nested/test_miniflux_pause_new.py", "tests/test_miniflux_pause*.py"))
        with self.assertRaises(ValueError):
            matches_path("README.md", "!README.md")


if __name__ == "__main__":
    unittest.main()
