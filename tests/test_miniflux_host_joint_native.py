import importlib.util
import base64
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("miniflux_host_joint_native", SCRIPTS / "miniflux_host_joint_native.py")
joint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(joint)


class JointHostProofTests(unittest.TestCase):
    def test_wrapper_nonzero_retains_exact_prefixes_and_process_identity(self):
        argv = [sys.executable, "-c", "import os; os.write(1,b'bad-json\\xff'); os.write(2,b'failure detail'); raise SystemExit(3)"]
        with tempfile.TemporaryDirectory(prefix="joint-wrapper-failed-") as directory:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                output, value = joint.capture_wrapper(process, Path(directory), {"run_id": "a" * 32})
                self.assertEqual(output, b"bad-json\xff")
                saved = json.loads((Path(directory) / "wrapper-process.json").read_text())
                self.assertEqual(saved, value)
                self.assertEqual(saved["pid"], process.pid)
                self.assertEqual(saved["argv"], argv)
                self.assertEqual(saved["returncode"], 3)
                self.assertEqual(saved["identity"], {"run_id": "a" * 32})
                self.assertEqual(base64.b64decode(saved["stderr"]["prefix_base64"]), b"failure detail")
                self.assertEqual(base64.b64decode(saved["stdout"]["prefix_base64"]), output)
                self.assertTrue(saved["stdout"]["eof_observed"])
                self.assertFalse(saved["timed_out"])
            finally:
                process.stdout.close()
                process.stderr.close()

    def test_wrapper_large_streams_drain_but_only_bounded_prefixes_retained(self):
        size = joint.WRAPPER_OUTPUT_LIMIT * 3
        argv = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x'*{size}); sys.stdout.flush(); sys.stderr.buffer.write(b'y'*{size}); sys.stderr.flush()"]
        with tempfile.TemporaryDirectory(prefix="joint-wrapper-large-") as directory:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                output, value = joint.capture_wrapper(process, Path(directory), {})
                self.assertEqual(len(output), joint.WRAPPER_OUTPUT_LIMIT)
                self.assertEqual(value["returncode"], 0)
                for name in ("stdout", "stderr"):
                    self.assertEqual(value[name]["captured_bytes"], joint.WRAPPER_OUTPUT_LIMIT)
                    self.assertEqual(value[name]["observed_bytes"], size)
                    self.assertTrue(value[name]["truncated"])
                    self.assertTrue(value[name]["eof_observed"])
            finally:
                process.stdout.close()
                process.stderr.close()

    def test_wrapper_timeout_keeps_partial_output_unknown_and_does_not_kill(self):
        read_fd, write_fd = os.pipe()
        argv = [sys.executable, "-c", f"import os; os.write(1,b'partial stdout'); os.write(2,b'partial stderr'); os.write({write_fd},b'1'); os.read(0,1)"]
        with tempfile.TemporaryDirectory(prefix="joint-wrapper-timeout-") as directory:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, pass_fds=(write_fd,))
            os.close(write_fd)
            try:
                self.assertTrue(joint.select.select([read_fd], [], [], 5)[0])
                self.assertEqual(os.read(read_fd, 1), b"1")
                with self.assertRaises(subprocess.TimeoutExpired):
                    joint.capture_wrapper(process, Path(directory), {"run_id": "b" * 32}, seconds=0.03)
                value = json.loads((Path(directory) / "wrapper-unconfirmed.json").read_text())
                self.assertEqual(value, json.loads((Path(directory) / "wrapper-process.json").read_text()))
                self.assertEqual(value["outcome"], "unknown_may_still_be_running")
                self.assertEqual(value["pid"], process.pid)
                self.assertIsNone(value["returncode"])
                self.assertIsNone(process.poll())
                self.assertTrue(value["timed_out"])
                self.assertFalse(value["killed"])
                self.assertFalse(value["retried"])
                for name in ("stdout", "stderr"):
                    self.assertEqual(base64.b64decode(value[name]["prefix_base64"]), ("partial " + name).encode())
                    self.assertFalse(value[name]["eof_observed"])
            finally:
                os.close(read_fd)
                # Test owner releases the deterministic synthetic fixture, not the harness.
                process.communicate(input=b"release", timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_child_timeout_is_retained_unknown_without_kill_or_retry(self):
        child = Mock(pid=123)
        child.communicate.side_effect = subprocess.TimeoutExpired("owned-synthetic", 0.01)
        with tempfile.TemporaryDirectory(prefix="joint-timeout-") as directory:
            root = Path(directory)
            with self.assertRaises(subprocess.TimeoutExpired):
                joint.release_child(child, root, time.monotonic() + 0.01)
            evidence = json.loads((root / "child-unconfirmed.json").read_text())
            self.assertEqual(evidence["outcome"], "unknown_may_still_be_running")
            self.assertFalse(evidence["killed"])
            self.assertFalse(evidence["retried"])
            child.communicate.assert_called_once()
            child.kill.assert_not_called()
            child.terminate.assert_not_called()

    def test_missing_newline_handshake_has_deadline(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, "rb") as stream:
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    joint.handshake(SimpleNamespace(stdout=stream), started + 0.03)
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(write_fd)

    def test_actual_old_cron_child_is_retained_closed_and_not_installed(self):
        with tempfile.TemporaryDirectory(prefix="joint-cron-") as directory:
            root = Path(directory) / "proof"
            result = joint.old_cron_proof(root, "a" * 32)
            self.assertEqual(result["actual_child_returncode"], 0)
            self.assertEqual(result["receipt"]["inflight"], 0)
            self.assertEqual(result["receipt"]["unknown"], 0)
            self.assertFalse(result["production_crontab_read_or_installed"])
            self.assertTrue((root / "gate/intent.json").is_file())
            self.assertEqual(len(list((root / "gate/runs").glob("*.json"))), 2)
            self.assertEqual(len(list((root / "gate/finishes").glob("*.json"))), 2)
            self.assertEqual(result["installed_wrapper"]["returncode"], 0)
            self.assertFalse(result["binding"]["installed_on_production"])
            captured = json.loads((root / "wrapper-process.json").read_text())
            self.assertEqual(captured["returncode"], 0)
            self.assertEqual(captured["timeout_seconds"], 5)
            self.assertEqual(captured["identity"]["run_id"], "a" * 32)
            self.assertEqual(len(captured["identity"]["installed_files_sha256"]), 2)
            self.assertFalse(captured["stdout"]["truncated"])


if __name__ == "__main__":
    unittest.main()
