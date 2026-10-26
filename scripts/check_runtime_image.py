#!/usr/bin/env python3
"""Validate the final image, not the CI runner; no model/network/database calls."""

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import sysconfig

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


FORBIDDEN = ("pip", "setuptools", "wheel", "pkg_resources", "ensurepip", "_distutils_hack")
RUNTIME_IMPORTS = (
    "ssl", "sqlite3", "gunicorn", "flask", "feedparser", "feedparser_sgmllib", "yaml",
    "openai", "httpx", "httpx2", "pydantic_core", "redis", "truststore",
    "app", "worker_main", "modules.a_stock_data", "modules.verified_close",
    "workers.market_close_collector", "workers.security_master_collector",
)
# Debian trixie fix floors verified on 2026-10-16; not a live vulnerability scan.
DEBIAN_FIX_FLOORS = {
    "libc6": "2.41-12+deb13u4",
    "gzip": "1.13-1+deb13u1",
    "openssl": "3.5.7-1~deb13u3",
    "libpcre2-8-0": "10.46-1~deb13u3",
    "perl-base": "5.40.1-6+deb13u1",
    "libsqlite3-0": "3.46.1-7+deb13u2",
    "libuuid1": "2.41.5-0+deb13u1",
    "liblzma5": "5.8.1-1+deb13u1",
}


# GHSA-6q48-6c7w-9gw2: .18 removed all emSign roots; .19 restores the
# unaffected roots. Inspect DER certificates, never bundle comments/names.
CERTIFI_FIXED_VERSION = "2026.10.19"
CERTIFI_DISTRUSTED_SHA256 = (
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
)
CERTIFI_RETAINED_ROOTS = {
    "emSign Root CA - G1": "40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367",
    "emSign Root CA - C1": "125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f",
    "emSign ECC Root CA - C3": "bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3",
    "ISRG Root X1": "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6",
    "DigiCert Global Root G2": "cb3ccbb76031e5e0138f8dd39a23f9de47ffc35e43c1144cea27d46a5ab1cb5f",
}


def certifi_bundle_report(version, bundle_path):
    """Inspect only certifi's actual store, without system roots or network I/O."""
    report = {"version": version, "errors": []}
    try:
        if Version(version) < Version(CERTIFI_FIXED_VERSION):
            report["errors"].append("certifi security fix version is below 2026.10.19")
    except (InvalidVersion, TypeError):
        report["errors"].append("certifi installed version is missing or invalid")
    try:
        with Path(bundle_path).open("rb") as stream:
            bundle = stream.read(2 * 1024 * 1024 + 1)
        if len(bundle) > 2 * 1024 * 1024:
            raise ValueError("oversized CA bundle")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=bundle.decode("ascii"))
        roots = {
            hashlib.sha256(der).hexdigest()
            for der in context.get_ca_certs(binary_form=True)
        }
    except (OSError, ValueError, UnicodeError, ssl.SSLError):
        report["errors"].append("certifi CA bundle is missing or unreadable")
        return report
    report.update(bundle_sha256=hashlib.sha256(bundle).hexdigest(), ca_count=len(roots))
    report["distrusted_root_present"] = CERTIFI_DISTRUSTED_SHA256 in roots
    report["retained_roots_present"] = {
        name: fingerprint in roots for name, fingerprint in CERTIFI_RETAINED_ROOTS.items()
    }
    if report["distrusted_root_present"]:
        report["errors"].append("certifi still contains the distrusted emSign ECC Root CA - G3 root")
    for name, present in report["retained_roots_present"].items():
        if not present:
            report["errors"].append(f"certifi unaffected trust root is missing: {name}")
    return report


def installed_certifi_report(versions):
    try:
        import certifi
        return certifi_bundle_report(versions.get("certifi"), certifi.where())
    except (ImportError, OSError):
        return {"version": versions.get("certifi"), "errors": ["certifi bundle unavailable"]}


def installed_versions():
    return {
        canonicalize_name(dist.metadata["Name"]): dist.version
        for dist in importlib.metadata.distributions()
    }


def debian_version_errors(run=subprocess.run):
    """Use Debian's comparator, never upstream/PEP440 version ordering."""
    try:
        query = run(
            ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n", *DEBIAN_FIX_FLOORS],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if query.returncode:
            return ["Debian installed-version query failed"]
        versions = {}
        for line in query.stdout.splitlines():
            name, version = line.split("\t")
            versions[name.split(":", 1)[0]] = version
        errors = []
        for name, floor in DEBIAN_FIX_FLOORS.items():
            version = versions.get(name)
            if not version:
                errors.append(f"Debian package missing: {name}")
                continue
            compared = run(
                ["dpkg", "--compare-versions", version, "ge", floor],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if compared.returncode:
                errors.append(f"Debian fix floor unmet or unverifiable: {name} {version}, required>={floor}")
        return errors
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ["Debian installed-version check failed"]


def dependency_errors(requirement_lines, versions, distributions):
    """Check pins and active runtime dependencies without needing pip check."""
    errors = []
    requirements = []
    for line in requirement_lines:
        line = line.strip()
        if line and not line.startswith("#"):
            requirements.append(("production lock", Requirement(line)))
    for dist in distributions:
        for text in dist.requires or ():
            requirements.append((dist.metadata["Name"], Requirement(text)))
    for owner, req in requirements:
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(req.name)
        version = versions.get(name)
        if version is None or not req.specifier.contains(version, prereleases=True):
            errors.append(f"{owner}: {req}, installed={version}")
    return errors


def packaging_errors(versions, site_roots, stdlib, scripts, find_spec=importlib.util.find_spec):
    """Also reject files left behind after a metadata-only removal."""
    errors = []
    for module in FORBIDDEN:
        if canonicalize_name(module) in versions:
            errors.append(f"packaging distribution remains: {module}")
        if find_spec(module) is not None:
            errors.append(f"packaging module remains: {module}")
    for root in site_roots:
        for name in FORBIDDEN:
            if (root / name).exists():
                errors.append(f"packaging tree remains: {root / name}")
        if (root / "distutils-precedence.pth").exists():
            errors.append("setuptools import hook remains")
        for pattern in ("pip-*.dist-info", "setuptools-*.dist-info", "wheel-*.dist-info"):
            if list(root.glob(pattern)):
                errors.append(f"packaging metadata remains: {pattern}")
    if (stdlib / "ensurepip").exists():
        errors.append("ensurepip (including bundled wheels) remains")
    for path in scripts.iterdir():
        if re.fullmatch(r"(?:pip(?:\d+(?:\.\d+)?)?|wheel|easy_install(?:-\d+\.\d+)?)", path.name):
            errors.append(f"packaging executable remains: {path.name}")
    return errors


def main():
    versions = installed_versions()
    paths = sysconfig.get_paths()
    sites = {Path(paths[key]) for key in ("purelib", "platlib")}
    errors = dependency_errors(
        Path("requirements-prod.txt").read_text().splitlines(),
        versions, importlib.metadata.distributions(),
    )
    errors += packaging_errors(versions, sites, Path(paths["stdlib"]), Path(paths["scripts"]))
    errors += debian_version_errors()
    certifi_report = installed_certifi_report(versions)
    errors += certifi_report["errors"]
    for executable in ("pip", "pip3", "wheel", "easy_install"):
        if shutil.which(executable):
            errors.append(f"packaging executable on PATH: {executable}")
    imported = []
    if not errors:
        for module in RUNTIME_IMPORTS:
            try:
                importlib.import_module(module)
                imported.append(module)
            except Exception as exc:
                errors.append(f"runtime import {module}: {type(exc).__name__}")
    print(json.dumps({"python": sys.version, "packages": versions,
                      "runtime_imports": imported, "certifi_bundle": certifi_report, "errors": errors}, sort_keys=True))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
