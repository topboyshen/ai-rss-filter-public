#!/usr/bin/env python3
"""Validate the final image, not the CI runner; no model/network/database calls."""

import importlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


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
                      "runtime_imports": imported, "errors": errors}, sort_keys=True))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
