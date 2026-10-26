"""Deployment wiring only: no containers, production, database or model calls."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
APP_ROLES = {"ai-filter-api", "ai-filter-worker", "market-close-collector",
             "event-enrichment", "security-master-sync"}


class AdmissionDeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]

    def test_every_application_execution_role_is_in_shared_fence_inventory(self):
        app_roles = {name for name, row in self.services.items() if row.get("build") == "."}
        self.assertEqual(app_roles, APP_ROLES)
        for name in app_roles:
            with self.subTest(role=name):
                env = self.services[name]["environment"]
                self.assertEqual(env["AI_FILTER_ADMISSION_ROOT"], "/app/data/admission_fence")
                self.assertEqual(env["AI_FILTER_DB_PATH"], "/app/data/cache.db")

    def test_default_disabled_is_literal_not_unaudited_env_interpolation(self):
        for name in APP_ROLES:
            with self.subTest(role=name):
                env = self.services[name]["environment"]
                self.assertIs(type(env["AI_FILTER_ADMISSION_MODE"]), str)
                self.assertEqual(env["AI_FILTER_ADMISSION_MODE"], "disabled")
                self.assertNotIn("AI_FILTER_ADMISSION_BINDING", env)
                self.assertNotIn("AI_FILTER_ADMISSION_DIR", env)  # No alternate fallback root.
                self.assertNotIn("env_file", self.services[name])

    def test_root_is_same_host_bind_not_private_container_or_readonly_mount(self):
        for name in APP_ROLES:
            with self.subTest(role=name):
                volumes = self.services[name]["volumes"]
                self.assertEqual(volumes.count("./data:/app/data"), 1)
                self.assertIn("release-registry-mask:/app/data/releases:ro", volumes)
                self.assertFalse(any("admission_fence" in str(volume) for volume in volumes))
                self.assertFalse(any("docker.sock" in str(volume) for volume in volumes))

    def test_non_application_containers_do_not_claim_application_fence_coverage(self):
        for name, service in self.services.items():
            if name not in APP_ROLES:
                with self.subTest(role=name):
                    self.assertFalse(any(key.startswith("AI_FILTER_ADMISSION_")
                                         for key in service.get("environment", {})))

    def test_immutable_image_and_existing_resource_security_limits_preserved(self):
        expected = {"ai-filter-api": ("768m", 256), "ai-filter-worker": ("768m", 256),
                    "event-enrichment": ("768m", 256), "market-close-collector": ("384m", 128),
                    "security-master-sync": ("384m", 128)}
        for name, limits in expected.items():
            with self.subTest(role=name):
                service = self.services[name]
                self.assertEqual(service["image"], "${IMAGE_REF:?set IMAGE_REF to an immutable version or sha256 digest}")
                self.assertEqual((service["mem_limit"], service["pids_limit"]), limits)
                self.assertIn("no-new-privileges:true", service["security_opt"])
                self.assertNotEqual(service.get("privileged"), True)

    def test_cron_profiles_remain_explicit_and_receive_same_manifest_path(self):
        self.assertEqual(self.services["market-close-collector"]["profiles"], ["cron"])
        self.assertEqual(self.services["event-enrichment"]["profiles"], ["cron"])
        self.assertEqual(self.services["security-master-sync"]["profiles"], ["security-master"])
        self.assertEqual(self.services["event-enrichment"]["network_mode"], "none")
        self.assertIs(self.services["security-master-sync"]["read_only"], True)


if __name__ == "__main__":
    unittest.main()
