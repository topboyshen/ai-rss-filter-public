"""Offline tests of actual certifi trust data; never contact TLS endpoints."""

import hashlib
import importlib.metadata
from pathlib import Path
import re
import shutil
import ssl
import subprocess

import certifi
import pytest

import scripts.check_runtime_image as checker


ROOT = Path(__file__).resolve().parents[1]
DISTRUSTED = ROOT / "tests/fixtures/certifi/emsign-ecc-root-g3.pem"


def _actual_bundle():
    return Path(certifi.where()).read_text(encoding="ascii")


def _clean_trust_fixture():
    # Unit-test control derived from public certificate data, not evidence that
    # an old installed package is fixed. The installed-package test stays raw.
    bundle = _actual_bundle()
    for pem in re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle, re.S):
        if hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest() == checker.CERTIFI_DISTRUSTED_SHA256:
            bundle = bundle.replace(pem, "")
    return bundle


def _write_bundle(tmp_path, text):
    path = tmp_path / "cacert.pem"
    path.write_text(text, encoding="ascii")
    return path


def _root_pem(bundle, fingerprint):
    for pem in re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle, re.S):
        if hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest() == fingerprint:
            return pem
    raise AssertionError(f"expected actual public root is absent: {fingerprint}")


def test_installed_package_and_actual_bundle_meet_security_fix():
    version = importlib.metadata.version("certifi")
    assert f"certifi=={version}" in (ROOT / "requirements-prod.txt").read_text().splitlines()
    assert version == certifi.__version__ == "2026.10.19"
    report = checker.installed_certifi_report({"certifi": version})
    assert report["errors"] == []
    assert report["distrusted_root_present"] is False
    assert all(report["retained_roots_present"].values())
    assert report["ca_count"] == 120
    assert report["bundle_sha256"] == hashlib.sha256(Path(certifi.where()).read_bytes()).hexdigest()


@pytest.mark.parametrize("version", ["2026.7.22", "2026.10.18", None, "invalid"])
def test_old_or_unverifiable_version_rejected_even_with_clean_bundle(tmp_path, version):
    path = _write_bundle(tmp_path, _clean_trust_fixture())
    report = checker.certifi_bundle_report(version, path)
    assert report["errors"]
    assert any("version" in error for error in report["errors"])
    assert report["distrusted_root_present"] is False


def test_fixed_metadata_cannot_hide_actual_distrusted_certificate(tmp_path):
    # No labels or comments: recognition must use decoded certificate bytes.
    pem = DISTRUSTED.read_text(encoding="ascii")
    assert hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest() == checker.CERTIFI_DISTRUSTED_SHA256
    path = _write_bundle(tmp_path, _clean_trust_fixture() + "\n" + pem)
    report = checker.certifi_bundle_report("2026.10.19", path)
    assert report["distrusted_root_present"] is True
    assert report["errors"] == ["certifi still contains the distrusted emSign ECC Root CA - G3 root"]


def test_bundle_comments_are_not_trust_certificates(tmp_path):
    path = _write_bundle(tmp_path, "# emSign ECC Root CA - G3 is distrusted\n" + _clean_trust_fixture())
    assert checker.certifi_bundle_report("2026.10.19", path)["errors"] == []


@pytest.mark.parametrize("name,fingerprint", list(checker.CERTIFI_RETAINED_ROOTS.items()))
def test_removing_unaffected_root_is_rejected(tmp_path, name, fingerprint):
    bundle = _clean_trust_fixture()
    path = _write_bundle(tmp_path, bundle.replace(_root_pem(bundle, fingerprint), ""))
    report = checker.certifi_bundle_report("2026.10.19", path)
    assert report["errors"] == [f"certifi unaffected trust root is missing: {name}"]
    assert report["retained_roots_present"][name] is False


@pytest.mark.parametrize("content", [
    "", "not a CA store", "-----BEGIN CERTIFICATE-----\nBAD\n-----END CERTIFICATE-----",
    "x" * (2 * 1024 * 1024 + 1),
], ids=["empty", "not-pem", "malformed-pem", "oversized"])
def test_unreadable_or_invalid_actual_bundle_fails_closed(tmp_path, content):
    path = _write_bundle(tmp_path, content)
    assert checker.certifi_bundle_report("2026.10.19", path)["errors"] == [
        "certifi CA bundle is missing or unreadable",
    ]


def test_missing_actual_bundle_fails_closed(tmp_path):
    assert checker.certifi_bundle_report("2026.10.19", tmp_path / "absent")["errors"]


@pytest.mark.parametrize("case,accepted", [("distrusted", False), ("old_trust", True), ("unaffected", True)])
def test_openssl_verifies_normal_trust_but_rejects_removed_root(tmp_path, case, accepted):
    # A self-signed public root verifies only if the chosen trust store trusts it.
    executable = shutil.which("openssl")
    assert executable, "OpenSSL is required for the offline trust regression"
    bundle = _clean_trust_fixture()
    pem = DISTRUSTED.read_text(encoding="ascii")
    if case == "old_trust":
        bundle += "\n" + pem
    elif case == "unaffected":
        pem = _root_pem(bundle, checker.CERTIFI_RETAINED_ROOTS["ISRG Root X1"])
    ca = _write_bundle(tmp_path, bundle)
    root = tmp_path / "root.pem"
    root.write_text(pem, encoding="ascii")
    empty_ca_dir = tmp_path / "empty-ca-dir"
    empty_ca_dir.mkdir()
    result = subprocess.run(
        [executable, "verify", "-CAfile", str(ca), "-CApath", str(empty_ca_dir), str(root)],
        capture_output=True, text=True, timeout=3, check=False,
    )
    if accepted:
        assert result.returncode == 0, result.stdout + result.stderr
        assert ": OK" in result.stdout
    else:
        assert result.returncode != 0
        assert "error 18" in result.stdout + result.stderr
        assert re.search(r"self[- ]signed certificate", result.stdout + result.stderr)
