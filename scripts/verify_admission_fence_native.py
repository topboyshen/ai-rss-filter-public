"""Native AMD64 offline admission isolation, never a production controller.

Reuse the fixed image and strict resource/inspect/evidence implementation.
Only the bound source inventory and stdlib test suite differ. HTTP integration
uses fake transport in the separate full CI job; this suite proves real local
cross-process locks and persisted fence transitions without network access.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def runner():
    source = Path(__file__).with_name("verify_handoff_operations_native.py")
    spec = importlib.util.spec_from_file_location("admission_native_base", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FILES = (
    "scripts/verify_admission_fence_native.py", "scripts/verify_handoff_operations_native.py",
    "services/admission_fence.py", "services/admission_http.py",
    "scripts/control_admission_fence.py", "tests/test_admission_fence.py",
    "tests/test_admission_fence_control.py", "tests/test_admission_fence_deployment_contract.py",
    "tests/test_admission_http.py", "tests/test_admission_background.py", "tests/test_admission_native.py",
    "app.py", "docker-compose.yml", "worker_main.py", "routes/webhook_routes.py",
    "routes/dashboard_routes.py", "services/refresh.py", "services/quant_market_context.py",
    "services/dashboard_snapshot_updates.py", "services/first_rejection_retention.py",
    "scripts/run_db_maintenance.py", "scripts/run_event_enrichment.py",
    "workers/market_close_collector.py", "workers/security_master_collector.py",
)
CONTAINER_TEST = """
import json, pathlib, platform, sys, unittest
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
suite = unittest.TestSuite()
for pattern in ('test_admission_native.py', 'test_admission_fence_control.py'):
    suite.addTests(unittest.defaultTestLoader.discover('/work/tests', pattern=pattern))
assert suite.countTestCases() >= 16
result = unittest.TextTestRunner(verbosity=2).run(suite)
peak = pathlib.Path('/sys/fs/cgroup/memory.peak')
events = pathlib.Path('/sys/fs/cgroup/memory.events')
print(json.dumps({'status': 'passed' if result.wasSuccessful() else 'failed',
      'tests_run': result.testsRun, 'failures': len(result.failures),
      'errors': len(result.errors), 'skipped': len(result.skipped),
      'memory_peak_bytes': int(peak.read_text()) if peak.exists() else None,
      'memory_events': events.read_text() if events.exists() else None,
      'machine': platform.machine(), 'production_access': False, 'model_calls': 0}))
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    runner().run(options.repo.resolve(), options.output.resolve(), files=FILES, container_test=CONTAINER_TEST)


if __name__ == "__main__":
    main()
