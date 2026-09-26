# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Python supply-chain detector backed by the OSV batch API.

OSV is queried directly rather than through ``pip-audit`` so the detector does
not need a resolvable virtualenv for the pinned set — it reads the pins out of
``requirements/base.txt`` and asks OSV about exactly those versions.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator, TypedDict

import requests

from ..models import Finding, Severity
from .base import register

logger = logging.getLogger(__name__)

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{vuln_id}"
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;#]+)")
REQUIREMENTS = "requirements/base.txt"


class OsvEvent(TypedDict, total=False):
    fixed: str


class OsvRange(TypedDict, total=False):
    events: list[OsvEvent]


class OsvPackage(TypedDict, total=False):
    name: str


class OsvAffected(TypedDict, total=False):
    package: OsvPackage
    ranges: list[OsvRange]


class OsvDatabaseSpecific(TypedDict, total=False):
    cvss_score: float


class OsvVuln(TypedDict, total=False):
    id: str
    summary: str
    affected: list[OsvAffected]
    database_specific: OsvDatabaseSpecific


SEVERITY_BY_CVSS = (
    (9.0, Severity.CRITICAL),
    (7.0, Severity.HIGH),
    (4.0, Severity.MODERATE),
)


def _parse_pins(path: Path) -> list[tuple[str, str]]:
    pins = []
    for line in path.read_text().splitlines():
        match = PIN.match(line.strip())
        if match:
            pins.append((match.group(1), match.group(2)))
    return pins


def _severity(vuln: OsvVuln) -> Severity:
    # OSV exposes a numeric score only in the database_specific block for some
    # ecosystems; fall back to HIGH, which is the conservative default for a
    # runtime dependency of a web application.
    numeric = (vuln.get("database_specific") or {}).get("cvss_score")
    if isinstance(numeric, (int, float)):
        for threshold, severity in SEVERITY_BY_CVSS:
            if numeric >= threshold:
                return severity
        return Severity.LOW
    return Severity.HIGH


def _fix_versions(vuln: OsvVuln, package: str) -> list[str]:
    fixes: list[str] = []
    for affected in vuln.get("affected", []) or []:
        if (affected.get("package") or {}).get("name") != package:
            continue
        for rng in affected.get("ranges", []) or []:
            for event in rng.get("events", []) or []:
                if "fixed" in event:
                    fixes.append(event["fixed"])
    return sorted(set(fixes))


@register("osv_python")
def detect(repo: Path) -> Iterator[Finding]:
    requirements = repo / REQUIREMENTS
    if not requirements.exists():
        logger.warning("%s missing; skipping osv_python", REQUIREMENTS)
        return
    pins = _parse_pins(requirements)
    queries = [
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
        for name, version in pins
    ]
    response = requests.post(OSV_BATCH, json={"queries": queries}, timeout=120)
    response.raise_for_status()
    results = response.json().get("results", [])

    for (name, version), result in zip(pins, results, strict=False):
        vulns = result.get("vulns") or []
        if not vulns:
            continue
        details = []
        severity = Severity.LOW
        fixes: set[str] = set()
        for stub in vulns:
            vuln = requests.get(OSV_VULN.format(vuln_id=stub["id"]), timeout=60).json()
            vuln_severity = _severity(vuln)
            if (
                vuln_severity.value == Severity.CRITICAL.value
                or severity == Severity.LOW
            ):
                severity = vuln_severity
            fixes.update(_fix_versions(vuln, name))
            details.append(
                f"- **{vuln.get('id')}**: {(vuln.get('summary') or '').strip()}\n"
                f"  https://osv.dev/vulnerability/{vuln.get('id')}"
            )
        fix_hint = ", ".join(sorted(fixes)) or "see the advisories"
        yield Finding(
            detector="osv_python",
            key=f"{name}=={version}",
            title=(
                f"security(deps): {name} {version} is affected by "
                f"{len(vulns)} advisory(ies)"
            ),
            severity=severity,
            wave="A",
            labels=["devin-fix", "security", "dependencies"],
            body=(
                f"`{REQUIREMENTS}` pins `{name}=={version}`, which OSV reports as "
                f"vulnerable. Fixed in: {fix_hint}.\n\n"
                f"### Advisories\n\n" + "\n".join(details)
            ),
            reproduce=(
                "python -m devin_pipeline.pipeline.cli --dry-run detect "
                "--only osv_python"
            ),
            acceptance=[
                f"`{name}` is pinned to a version OSV reports as unaffected",
                "Every requirements file that pins the package is updated consistently",
                "`pytest tests/unit_tests` passes",
            ],
            metadata={
                "package": name,
                "version": version,
                "fix_versions": sorted(fixes),
            },
        )
