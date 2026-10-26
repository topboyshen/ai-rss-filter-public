"""CI scope regression tests; runnable with stdlib unittest, without conftest.

    python -m unittest discover -s tests -p test_ci_change_scope.py

Temporary Git repositories exercise real diffs rather than replacing Git with
handwritten filename lists. The tests never contact a remote or an application.
"""

from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from scripts import ci_change_scope as scope


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci_change_scope.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class GitScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ci-scope-regression-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "GITHUB_"))
        }
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-q")
        for key, value in (
            ("user.name", "CI Scope Test"),
            ("user.email", "ci-scope@example.invalid"),
            ("core.hooksPath", os.devnull),
            ("core.fileMode", "true"),
            ("commit.gpgSign", "false"),
        ):
            self.git("config", key, value)
        self.write("README.md", "# Project\n")
        self.write("docs/guide.md", "# Guide\n")
        self.write("core/example.py", "VALUE = 1\n")
        self.base = self.commit()

    def git(self, *args):
        result = subprocess.run(
            [
                "git",
                # Commit may return while detached maintenance still writes
                # .git. Keep these disposable fixtures strictly synchronous;
                # command scope also covers init and overrides local config.
                "-c", "maintenance.auto=false",
                "-c", "gc.auto=0",
                *args,
            ],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()

    def write(self, name, contents="# Added document\n"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "scope regression fixture")
        return self.git("rev-parse", "HEAD")

    def event(self, *, base=None, head=None):
        repository = {"full_name": "owner/project"}
        return {
            "number": 23,
            "repository": repository.copy(),
            "pull_request": {
                "number": 23,
                "state": "open",
                "base": {"sha": base or self.base, "repo": repository.copy()},
                "head": {
                    "sha": head or self.git("rev-parse", "HEAD"),
                    "repo": {"full_name": "contributor/fork"},
                },
            },
        }

    def classify(self, event=None, *, event_name="pull_request"):
        return scope.classify_scope(
            self.repo, event_name, self.event() if event is None else event
        )

    def test_fixture_disables_auto_writers_even_if_local_config_enables_them(self):
        self.git("config", "--local", "maintenance.auto", "true")
        self.git("config", "--local", "gc.auto", "1")
        self.assertEqual(self.git("config", "--bool", "maintenance.auto"), "false")
        self.assertEqual(self.git("config", "--int", "gc.auto"), "0")
        # The override is per command, not a change to the real Git defaults.
        self.assertEqual(self.git("config", "--local", "maintenance.auto"), "true")
        self.assertEqual(self.git("config", "--local", "gc.auto"), "1")

    def test_fixture_commit_finishes_without_starting_detached_maintenance(self):
        self.git("config", "--local", "maintenance.auto", "true")
        self.git("config", "--local", "maintenance.strategy", "incremental")
        self.git("config", "--local", "gc.auto", "1")
        with tempfile.TemporaryDirectory(prefix="ci-scope-trace-") as directory:
            trace = Path(directory) / "git.jsonl"
            self.env["GIT_TRACE2_EVENT"] = str(trace)
            try:
                self.write("docs/committed.md", "# A real commit\n")
                head = self.commit()
                self.assertNotEqual(head, self.base)
                self.assertEqual(
                    self.git("show", f"{head}:docs/committed.md"), "# A real commit"
                )
            finally:
                self.env.pop("GIT_TRACE2_EVENT", None)
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertTrue(any(event.get("name") == "commit" for event in events))
            writers = [
                event for event in events
                if event.get("name") in {"maintenance", "gc"}
                or (
                    event.get("event") == "child_start"
                    and any(arg in {"maintenance", "gc"} for arg in event.get("argv", []))
                )
            ]
            self.assertEqual(writers, [], "Temporary Git fixture launched an auto writer")

    def test_fixture_git_command_errors_still_fail(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("rev-parse", "--verify", "refs/heads/fixture-does-not-exist")

    def test_regular_allowlisted_markdown_additions_and_edits_are_lightweight(self):
        for name in (
            "README.md", "CONTRIBUTING.md", "docs/nested/guide.md",
            "docs/发布说明 with spaces.md",
        ):
            with self.subTest(name=name):
                base = self.git("rev-parse", "HEAD")
                self.write(name, "# Updated\n\nText with `code` and 中文。\n")
                head = self.commit()
                result = self.classify(self.event(base=base, head=head))
                self.assertTrue(result.docs_only, result.reason)
                self.assertEqual(result.paths, (name,))
                self.assertEqual(scope.check_documents(self.repo, result), [])

    def test_code_config_workflows_and_nonallowlisted_markdown_require_full(self):
        names = (
            "core/example.py",
            "tests/example.py",
            "scripts/example.sh",
            ".github/workflows/ci.yml",
            "config.yaml",
            "requirements.txt",
            "requirements-prod.txt",
            "requirements-lint.txt",
            "pytest.ini",
            ".gitignore",
            "Dockerfile",
            "docker-compose.yml",
            "ops/README.md",
            "core/README.md",
            "scripts/README.md",
            "docs/lint-baseline.md",
            "docs/example.py",
            "docs/example.json",
            "docs/.hidden.md",
            "docs/UPPER.MD",
        )
        for name in names:
            with self.subTest(name=name):
                base = self.git("rev-parse", "HEAD")
                self.write(name, "changed\n")
                self.commit()
                self.assertFalse(self.classify(self.event(base=base)).docs_only)

    def test_mixed_code_and_documentation_require_full(self):
        self.write("README.md", "# Changed\n")
        self.write("core/example.py", "VALUE = 2\n")
        self.commit()
        self.assertFalse(self.classify().docs_only)

    def test_push_schedule_dispatch_and_unknown_events_never_skip_full_gate(self):
        self.write("README.md", "# Changed\n")
        self.commit()
        for event_name in ("push", "schedule", "workflow_dispatch", "", "other"):
            with self.subTest(event_name=event_name):
                self.assertFalse(self.classify(event_name=event_name).docs_only)

    def test_empty_diff_is_not_documentation_only(self):
        self.assertFalse(self.classify().docs_only)

    def test_invalid_or_missing_metadata_is_conservative(self):
        self.write("README.md", "# Changed\n")
        self.commit()
        original = self.event()
        changes = (("pull_request", None), ("repository", None))
        for key, value in changes:
            with self.subTest(key=key, value=value):
                event = copy.deepcopy(original)
                event[key] = value
                self.assertFalse(self.classify(event).docs_only)
        for number in (None, True, 0, -1, "23"):
            with self.subTest(number=number):
                event = copy.deepcopy(original)
                event["pull_request"]["number"] = number
                self.assertFalse(self.classify(event).docs_only)
        event = copy.deepcopy(original)
        event["number"] = 24
        self.assertFalse(self.classify(event).docs_only)
        for side in ("base", "head"):
            for sha in ("", "HEAD", "a" * 39, "z" * 40, "-" + "a" * 39):
                with self.subTest(side=side, sha=sha):
                    event = copy.deepcopy(original)
                    event["pull_request"][side]["sha"] = sha
                    self.assertFalse(self.classify(event).docs_only)

    def test_wrong_repository_closed_pr_and_wrong_checkout_require_full(self):
        self.write("README.md", "# Changed\n")
        head = self.commit()
        event = self.event()
        event["pull_request"]["base"]["repo"]["full_name"] = "other/project"
        self.assertFalse(self.classify(event).docs_only)
        event = self.event()
        event["pull_request"]["state"] = "closed"
        self.assertFalse(self.classify(event).docs_only)
        self.git("checkout", "-q", "--detach", self.base)
        self.assertFalse(self.classify(self.event(head=head)).docs_only)

    def test_missing_commit_and_git_failure_require_full(self):
        self.write("README.md", "# Changed\n")
        self.commit()
        event = self.event(base="a" * 40)
        self.assertFalse(self.classify(event).docs_only)
        event = self.event()
        with mock.patch.object(
            scope.subprocess, "run", side_effect=OSError("git unavailable")
        ):
            self.assertFalse(self.classify(event).docs_only)
        with mock.patch.object(
            scope.subprocess, "run",
            side_effect=subprocess.TimeoutExpired("git", 15),
        ):
            self.assertFalse(self.classify(event).docs_only)

    def test_all_pr_commits_are_checked_not_just_the_latest_docs_commit(self):
        self.write("core/example.py", "VALUE = 2\n")
        self.commit()
        self.write("README.md", "# New final documentation commit\n")
        self.commit()
        self.assertFalse(self.classify().docs_only)

    def test_diverged_base_uses_merge_base_not_target_branch_only_changes(self):
        self.git("checkout", "-qb", "feature")
        self.write("docs/first.md")
        self.commit()
        self.write("docs/second.md")
        head = self.commit()
        self.git("checkout", "-qb", "advanced-base", self.base)
        self.write("core/example.py", "VALUE = 3\n")
        advanced_base = self.commit()
        self.git("checkout", "-q", "feature")
        result = self.classify(self.event(base=advanced_base, head=head))
        self.assertTrue(result.docs_only, result.reason)
        self.assertEqual(result.merge_base, self.base)
        self.assertEqual(set(result.paths), {"docs/first.md", "docs/second.md"})

    def test_exact_github_merge_checkout_is_supported_without_changing_head(self):
        self.git("checkout", "-qb", "feature")
        self.write("docs/feature.md")
        head = self.commit()
        self.git("checkout", "-qb", "target", self.base)
        self.git("commit", "--allow-empty", "-qm", "target metadata only")
        base = self.git("rev-parse", "HEAD")
        self.git("merge", "--no-ff", "-qm", "synthetic PR merge", "feature")
        checkout = self.git("rev-parse", "HEAD")
        result = self.classify(self.event(base=base, head=head))
        self.assertTrue(result.docs_only, result.reason)
        self.assertEqual(result.paths, ("docs/feature.md",))
        self.assertEqual(scope.check_documents(self.repo, result), [])
        self.assertEqual(self.git("rev-parse", "HEAD"), checkout)
        wrong_base = self.event(base=self.base, head=head)
        self.assertFalse(self.classify(wrong_base).docs_only)

    def test_merge_checkout_with_additional_base_code_keeps_full_gate(self):
        self.git("checkout", "-qb", "feature")
        self.write("docs/feature.md")
        head = self.commit()
        self.git("checkout", "-qb", "target", self.base)
        self.write("core/example.py", "VALUE = 3\n")
        base = self.commit()
        self.git("merge", "--no-ff", "-qm", "synthetic PR merge", "feature")
        self.assertFalse(self.classify(self.event(base=base, head=head)).docs_only)

    def test_merge_checkout_with_additional_base_document_keeps_full_gate(self):
        self.git("checkout", "-qb", "feature")
        self.write("docs/feature.md")
        head = self.commit()
        self.git("checkout", "-qb", "target", self.base)
        self.write("docs/base-only.md", "# Not in the PR head\n\x00")
        base = self.commit()
        self.git("merge", "--no-ff", "-qm", "synthetic PR merge", "feature")
        self.assertFalse(self.classify(self.event(base=base, head=head)).docs_only)

    def test_renames_and_deletions_conservatively_keep_full_gate(self):
        for source, destination in (
            ("docs/guide.md", "docs/renamed.md"),
            ("README.md", "core/README.md"),
            ("core/example.py", "docs/example.md"),
        ):
            with self.subTest(source=source, destination=destination):
                base = self.git("rev-parse", "HEAD")
                (self.repo / destination).parent.mkdir(parents=True, exist_ok=True)
                self.git("mv", "--", source, destination)
                self.commit()
                self.assertFalse(self.classify(self.event(base=base)).docs_only)
        base = self.git("rev-parse", "HEAD")
        self.git("rm", "--", "docs/renamed.md")
        self.commit()
        self.assertFalse(self.classify(self.event(base=base)).docs_only)

    def test_symlink_and_executable_markdown_are_not_lightweight(self):
        base = self.git("rev-parse", "HEAD")
        (self.repo / "docs" / "linked.md").symlink_to("../README.md")
        self.commit()
        self.assertFalse(self.classify(self.event(base=base)).docs_only)
        base = self.git("rev-parse", "HEAD")
        self.write("docs/executable.md")
        self.git("add", "--", "docs/executable.md")
        self.git("update-index", "--chmod=+x", "docs/executable.md")
        self.git("commit", "-qm", "executable markdown fixture")
        self.assertFalse(self.classify(self.event(base=base)).docs_only)

    def test_control_characters_and_workflow_command_filenames_fail_closed(self):
        for name in (
            "docs/line\n::error::injected.md",
            "docs/tab\tname.md",
            "docs/carriage\rname.md",
        ):
            with self.subTest(name=repr(name)):
                base = self.git("rev-parse", "HEAD")
                self.write(name)
                self.commit()
                self.assertFalse(self.classify(self.event(base=base)).docs_only)

    def test_documentation_checker_rejects_unreadable_and_binary_documents(self):
        self.write("docs/binary.md", "header\x00body\n")
        self.commit()
        result = self.classify()
        self.assertTrue(result.docs_only)
        self.assertTrue(scope.check_documents(self.repo, result))
        with mock.patch.object(
            scope.subprocess, "run", side_effect=OSError("git unavailable")
        ):
            self.assertTrue(scope.check_documents(self.repo, result))

    def test_documentation_checker_reads_committed_blob_not_working_tree(self):
        self.write("docs/committed.md", "# Sound content\n")
        self.commit()
        result = self.classify()
        self.write("docs/committed.md", "<<<<<<< uncommitted\ninvalid\x00\n")
        self.assertEqual(scope.check_documents(self.repo, result), [])

    def test_documentation_check_rejects_conflicts_and_bad_utf8(self):
        for contents in (
            b"<<<<<<< ours\ntext\n=======\nother\n>>>>>>> theirs\n",
            b"<<<<<<<< ours\ntext\n========\nother\n>>>>>>>> theirs\n",
            b"# Bad UTF-8\n\xff\n",
            b"# Trailing whitespace \t\n",
        ):
            with self.subTest(contents=repr(contents)):
                base = self.git("rev-parse", "HEAD")
                path = self.repo / "docs" / "changed.md"
                path.write_bytes(contents)
                self.commit()
                result = self.classify(self.event(base=base))
                self.assertTrue(result.docs_only)
                self.assertTrue(scope.check_documents(self.repo, result))

    def test_documented_conflict_marker_examples_inside_fences_are_allowed(self):
        self.write(
            "docs/examples.md",
            "# Conflict examples\n\n```text\n<<<<<<< ours\n=======\n"
            ">>>>>>> theirs\n```\n\n~~~\n<<<<<<< example\n~~~\n",
        )
        self.commit()
        self.assertEqual(scope.check_documents(self.repo, self.classify()), [])

    def test_markdown_two_space_line_break_is_not_a_whitespace_failure(self):
        self.write("docs/line-break.md", "# Title\n\nFirst line  \nSecond line\n")
        self.commit()
        self.assertEqual(scope.check_documents(self.repo, self.classify()), [])

    def test_cli_writes_only_a_fixed_scope_result_not_untrusted_paths(self):
        self.write("docs/line\n::error::injected.md")
        self.commit()
        event_path = self.repo / "event.json"
        event_path.write_text(json.dumps(self.event()), encoding="utf-8")
        output = self.repo / "github-output"
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "classify",
                "--event-name", "pull_request", "--event-path", str(event_path),
                "--repo", str(self.repo), "--output", str(output),
            ],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("::error::injected", result.stdout + result.stderr)
        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["docs_only=false"])

    def run_workflow_scope_step(self, *, base=None, event_name="pull_request"):
        """Execute the workflow's actual classifier shell against our test repo."""
        event = self.event(base=base)
        event_path = self.repo / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        runner_temp = self.repo / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        output = self.repo / "github-output"
        step = _step_block(
            _job_blocks(WORKFLOW.read_text(encoding="utf-8"))["lint"],
            "Classify pull request scope using the base revision",
        )
        command = textwrap.dedent(step.split("        run: |\n", 1)[1])
        env = {
            **self.env,
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{self.env.get('PATH', '')}",
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_OUTPUT": str(output),
            "PR_BASE_SHA": event["pull_request"]["base"]["sha"],
            "RUNNER_TEMP": str(runner_temp),
        }
        result = subprocess.run(
            ["bash", "-c", command], cwd=self.repo, env=env,
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_text(encoding="utf-8")

    def test_workflow_first_introduction_without_base_helper_runs_full(self):
        self.write("scripts/ci_change_scope.py", SCRIPT.read_text(encoding="utf-8"))
        self.write("docs/change.md")
        self.commit()
        self.assertEqual(self.run_workflow_scope_step(), "docs_only=false\n")

    def test_workflow_uses_base_helper_for_verified_documentation(self):
        self.write("scripts/ci_change_scope.py", SCRIPT.read_text(encoding="utf-8"))
        base = self.commit()
        self.write("docs/change.md")
        self.commit()
        self.assertEqual(
            self.run_workflow_scope_step(base=base), "docs_only=true\n"
        )

    def test_workflow_modified_head_classifier_cannot_exempt_its_own_change(self):
        self.write("scripts/ci_change_scope.py", SCRIPT.read_text(encoding="utf-8"))
        base = self.commit()
        self.write(
            "scripts/ci_change_scope.py",
            "import sys\nfrom pathlib import Path\n"
            "Path(sys.argv[sys.argv.index('--output') + 1]).write_text("
            "'docs_only=true\\n')\n",
        )
        self.write("core/example.py", "VALUE = 2\n")
        self.write("docs/change.md")
        self.commit()
        self.assertEqual(
            self.run_workflow_scope_step(base=base), "docs_only=false\n"
        )

    def test_workflow_non_pr_events_never_execute_the_classifier(self):
        self.write(
            "scripts/ci_change_scope.py",
            "from pathlib import Path\n"
            "Path('must-not-exist').touch()\nraise RuntimeError('must not run')\n",
        )
        base = self.commit()
        self.write("docs/change.md")
        self.commit()
        for event_name in ("push", "schedule"):
            with self.subTest(event_name=event_name):
                output = self.repo / "github-output"
                output.unlink(missing_ok=True)
                self.assertEqual(
                    self.run_workflow_scope_step(base=base, event_name=event_name),
                    "docs_only=false\n",
                )
                self.assertFalse((self.repo / "must-not-exist").exists())

    def test_workflow_nonexact_output_cannot_enable_exemption(self):
        self.write(
            "scripts/ci_change_scope.py",
            "import sys\nfrom pathlib import Path\n"
            "Path(sys.argv[sys.argv.index('--output') + 1]).write_text("
            "'docs_only=true\\nuntrusted=field\\n')\n",
        )
        base = self.commit()
        self.write("docs/change.md")
        self.commit()
        self.assertEqual(
            self.run_workflow_scope_step(base=base), "docs_only=false\n"
        )

    def test_workflow_base_helper_failure_falls_back_to_full(self):
        self.write("scripts/ci_change_scope.py", "raise RuntimeError('failed')\n")
        base = self.commit()
        self.write("docs/change.md")
        self.commit()
        self.assertEqual(
            self.run_workflow_scope_step(base=base), "docs_only=false\n"
        )


def _job_blocks(text):
    """Read only top-level job boundaries, not a replacement YAML interpreter."""
    jobs = text.split("\njobs:\n", 1)[1]
    matches = list(re.finditer(r"^  ([A-Za-z_][\w-]*):\s*$", jobs, re.MULTILINE))
    return {
        match.group(1): jobs[
            match.end(): matches[index + 1].start() if index + 1 < len(matches) else None
        ]
        for index, match in enumerate(matches)
    }


def _step_block(job, name):
    """Keep assertions at a named step boundary instead of snapshotting YAML."""
    marker = f"      - name: {name}\n"
    body = job.split(marker, 1)[1]
    next_step = re.search(r"^      - (?:name|uses):", body, re.MULTILINE)
    return body[:next_step.start()] if next_step else body


class GitFixtureCleanupTests(unittest.TestCase):
    def test_late_git_writer_cleanup_error_is_not_ignored_or_retried(self):
        """Force the observed scan/rmdir race without scheduling real processes."""
        case = GitScopeTests(
            "test_diverged_base_uses_merge_base_not_target_branch_only_changes"
        )
        temporary_directory = tempfile.TemporaryDirectory
        rmdir = os.rmdir
        directories = []
        injected = []

        def track_directory(*args, **kwargs):
            directory = temporary_directory(*args, **kwargs)
            directories.append(directory)
            return directory

        def late_writer(path, *, dir_fd=None):
            if path == ".git" and not injected:
                # rmtree already scanned/removed the entries. A detached Git
                # writer can now create a lock before the final rmdir call.
                (case.repo / ".git" / "late-writer.lock").touch()
                injected.append(path)
            return rmdir(path, dir_fd=dir_fd)

        try:
            with (
                mock.patch.object(tempfile, "TemporaryDirectory", side_effect=track_directory),
                mock.patch.object(os, "rmdir", side_effect=late_writer),
            ):
                result = unittest.TestResult()
                case.run(result)
            self.assertEqual(injected, [".git"])
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(result.failures, [])
            self.assertEqual(len(result.errors), 1, result.errors)
            self.assertIn(f"[Errno {errno.ENOTEMPTY}]", result.errors[0][1])
            self.assertTrue((case.repo / ".git" / "late-writer.lock").is_file())
            self.assertFalse(result.wasSuccessful())
        finally:
            # Only the test's injected writer is gone here. Normal fixture
            # cleanup above must fail rather than retry or hide the error.
            for directory in directories:
                directory.cleanup()


class WorkflowScopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _job_blocks(cls.workflow)

    def test_required_lint_and_single_python_test_names_stay_stable(self):
        self.assertIn("lint", self.jobs)
        self.assertIn("test", self.jobs)
        self.assertIn("name: test (3.11)", self.jobs["test"])
        self.assertIn("python-version: '3.11'", self.jobs["test"])
        self.assertIn("name: coverage-python-3.11", self.jobs["test"])
        self.assertNotRegex(self.jobs["test"], r"(?m)^\s*(strategy|matrix):")
        self.assertNotIn("matrix.python-version", self.workflow)
        self.assertNotRegex(self.workflow, r"(?m)^\s*(paths|paths-ignore):")

    def test_master_push_and_scheduled_runs_are_still_triggered(self):
        triggers = self.workflow.split("\njobs:\n", 1)[0]
        self.assertRegex(triggers, r"(?m)^  push:\s*\n\s+branches: \[master\]")
        self.assertIn("  pull_request:", triggers)
        self.assertIn("  schedule:", triggers)
        self.assertRegex(triggers, r"cron: ['\"]0 0 \* \* 1['\"]")

    def test_only_runs_of_the_same_pr_can_cancel_each_other(self):
        configuration = self.workflow.split("\njobs:\n", 1)[0]
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
            configuration,
        )
        self.assertIn(
            "github.event_name == 'pull_request' && "
            "format('pr-{0}', github.event.pull_request.number) || github.run_id",
            configuration,
        )
        self.assertIn("github.workflow", configuration)

    def test_default_merge_checkout_and_fail_closed_full_test_gate(self):
        self.assertIn("fetch-depth: 0", self.jobs["lint"])
        self.assertNotRegex(self.jobs["lint"], r"(?m)^\s+ref:")
        self.assertNotRegex(self.jobs["test"], r"(?m)^\s+ref:")
        self.assertIn("name: test (3.11)", self.jobs["test"])
        self.assertIn("needs: lint", self.jobs["test"])
        self.assertIn(
            "if: ${{ !cancelled() && (needs.lint.result != 'success' || "
            "needs.lint.outputs.docs_only != 'true') }}",
            self.jobs["test"],
        )
        self.assertNotIn("continue-on-error:", self.jobs["test"])
        cases = (
            ("success", "true", False, False),
            ("failure", "true", False, True),
            ("success", "false", False, True),
            ("success", "", False, True),
            ("", "", False, True),
            ("failure", "false", True, False),
        )
        for lint_result, docs_only, cancelled, expected in cases:
            with self.subTest(result=lint_result, docs=docs_only, cancelled=cancelled):
                run_full = not cancelled and (
                    lint_result != "success" or docs_only != "true"
                )
                self.assertEqual(run_full, expected)

    def test_documentation_step_is_explicitly_lightweight_and_uses_trusted_checker(self):
        step = _step_block(
            self.jobs["lint"], "Check documentation and CI scope regressions"
        )
        self.assertIn("if: steps.scope.outputs.docs_only == 'true'", step)
        self.assertIn('"${RUNNER_TEMP}/ci-change-scope-trusted.py" check', step)
        self.assertIn(
            "python -m unittest discover -s tests -p test_ci_change_scope.py", step
        )
        self.assertNotIn("pip install", step)
        self.assertNotIn("docker build", step)
        self.assertNotIn("run-release-tests.sh full", step)
        self.assertIn("intentionally skipped", step)
        self.assertIn("GITHUB_STEP_SUMMARY", step)

    def test_full_gate_commands_and_runtime_image_validation_are_preserved(self):
        lint = self.jobs["lint"]
        tests = self.jobs["test"]
        self.assertIn("python scripts/check_lint.py", lint)
        for command in (
            "pip install -r requirements.txt",
            "bash scripts/check-shell-syntax.sh",
            "scripts/check_function_length.py --max 120",
            "./scripts/run-release-tests.sh full",
            "AI_FILTER_TEST_COVERAGE_OUTPUT=",
            "docker compose config --quiet",
            "docker build --tag",
            "scripts/check_runtime_image.py",
            "actions/upload-artifact@v7",
            "if-no-files-found: error",
        ):
            with self.subTest(command=command):
                self.assertIn(command, tests)


if __name__ == "__main__":
    unittest.main()
