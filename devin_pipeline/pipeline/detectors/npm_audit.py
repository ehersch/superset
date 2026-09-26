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
"""Frontend supply-chain detector backed by ``npm audit``.

Advisories are grouped by the *top-level package that has to be upgraded*
rather than filed one per CVE: sixteen of the twenty advisories on master
collapse into three upgrades, and three PRs are reviewable where twenty are
not.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from ..models import Finding, Severity
from .base import register, run

logger = logging.getLogger(__name__)

FRONTEND = "superset-frontend"
SEVERITY_MAP = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "moderate": Severity.MODERATE,
    "low": Severity.LOW,
}
RANK = {Severity.CRITICAL: 3, Severity.HIGH: 2, Severity.MODERATE: 1, Severity.LOW: 0}


def _fix_target(node: dict[str, Any]) -> str | None:
    fix = node.get("fixAvailable")
    if isinstance(fix, dict):
        return f"{fix['name']}@{fix['version']}"
    return None


def _advisory_titles(node: dict[str, Any]) -> list[str]:
    titles = []
    for via in node.get("via", []):
        if isinstance(via, dict) and via.get("title"):
            url = via.get("url", "")
            titles.append(f"{via['title']} ({url})" if url else via["title"])
    return titles


@register("npm_audit")
def detect(repo: Path) -> Iterator[Finding]:
    frontend = repo / FRONTEND
    if not (frontend / "package-lock.json").exists():
        logger.warning("no package-lock.json; skipping npm_audit")
        return
    result = run(
        ["npm", "audit", "--package-lock-only", "--json"], cwd=frontend, timeout=900
    )
    if not result.stdout.strip():
        logger.error("npm audit produced no output: %s", result.stderr[:300])
        return
    report = json.loads(result.stdout)

    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"packages": [], "severity": Severity.LOW, "advisories": []}
    )
    for name, node in (report.get("vulnerabilities") or {}).items():
        target = _fix_target(node)
        if target is None:
            # No published fix: a human has to decide between pinning,
            # patching or accepting the risk, so this is not agent work.
            continue
        group = groups[target]
        group["packages"].append(name)
        node_severity = SEVERITY_MAP.get(node.get("severity", "low"), Severity.LOW)
        if RANK[node_severity] > RANK[group["severity"]]:
            group["severity"] = node_severity
        group["advisories"].extend(_advisory_titles(node))

    for target, group in sorted(
        groups.items(), key=lambda kv: -RANK[kv[1]["severity"]]
    ):
        packages = sorted(set(group["packages"]))
        advisories = sorted(set(group["advisories"]))
        severity: Severity = group["severity"]
        advisory_list = "\n".join(f"- {item}" for item in advisories) or "- (no title)"
        package_list = ", ".join(f"`{pkg}`" for pkg in packages)
        yield Finding(
            detector="npm_audit",
            key=target,
            title=(
                f"security(frontend): upgrade to {target} to clear "
                f"{len(packages)} advisory chain package(s)"
            ),
            severity=severity,
            wave="A",
            labels=["devin-fix", "security", "dependencies"],
            body=(
                f"`npm audit` on `{FRONTEND}` reports vulnerable packages that are all "
                f"resolved by upgrading to **{target}**.\n\n"
                f"Affected packages in the dependency chain: {package_list}\n\n"
                f"Highest severity: **{severity.value}**\n\n"
                f"### Advisories\n\n{advisory_list}"
            ),
            reproduce=(
                f"cd {FRONTEND}\n"
                "npm audit --package-lock-only --json | "
                "jq '.vulnerabilities | with_entries("
                f'select(.value.fixAvailable.name == "{target.split("@")[0]}")'
                ")'"
            ),
            acceptance=[
                (
                    "`npm audit --package-lock-only` no longer reports the "
                    f"packages fixed by {target}"
                ),
                "No new advisory of equal or higher severity is introduced",
                "`npm run build` succeeds",
                "The Jest suites touching the upgraded packages pass",
            ],
            metadata={
                "fix_target": target,
                "packages": packages,
                "advisory_count": len(advisories),
            },
        )
