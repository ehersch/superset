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
"""Detect database engine specs that ship without user-facing metadata.

``engine_name`` is what the database-connection modal shows and what the
OAuth2 and error-message paths key on; ``sqlalchemy_uri_placeholder`` is the
example URI a user is shown while typing. A spec missing either is a spec that
presents as a blank or raw-identifier entry in the UI. The check is a pure AST
walk, so it runs without importing Superset or installing DB drivers.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

from ..models import Finding, Severity
from .base import register

SPEC_DIR = "superset/db_engine_specs"
REQUIRED = ("engine_name", "sqlalchemy_uri_placeholder")
# Base and mixin modules define the contract rather than a concrete engine.
SKIP_MODULES = {"base.py", "__init__.py", "lib.py", "exceptions.py"}


def _is_none(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _assigned_names(node: ast.ClassDef) -> set[str]:
    """Attributes the class sets to a real value.

    ``attr = None`` is how the base class declares the slot, not how a spec
    fills it, so it does not count.
    """
    names: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and not _is_none(stmt.value):
            names.update(
                target.id for target in stmt.targets if isinstance(target, ast.Name)
            )
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is not None and not _is_none(stmt.value):
                names.add(stmt.target.id)
    return names


def _is_engine_spec(node: ast.ClassDef) -> bool:
    """A concrete spec declares ``engine = "..."`` — that is its registry key."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "engine"
            for target in stmt.targets
        ):
            return isinstance(stmt.value, ast.Constant) and isinstance(
                stmt.value.value, str
            )
    return False


def _base_names(node: ast.ClassDef) -> list[str]:
    names = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _parse_classes(repo: Path) -> tuple[dict[str, ast.ClassDef], dict[str, str]]:
    """Map every class in the package to its node and its source module."""
    classes: dict[str, ast.ClassDef] = {}
    owners: dict[str, str] = {}
    for path in sorted((repo / SPEC_DIR).glob("*.py")):
        if path.name in SKIP_MODULES:
            # ``base.py`` supplies a generic fallback placeholder; inheriting
            # it is exactly the state this detector reports.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, node)
                owners.setdefault(node.name, path.name)
    return classes, owners


def scan_repo(repo: Path) -> list[tuple[str, str, list[str]]]:
    """Return ``(module, class, missing attributes)`` for incomplete specs.

    Attributes are resolved through the class hierarchy: engine specs are
    routinely declared as a base class plus one subclass per driver, and only
    the base carries the display name. Bases are matched by plain class name
    across the whole package, which is unambiguous here and avoids importing
    modules whose DB drivers are not installed.
    """
    classes, owners = _parse_classes(repo)

    def declared(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        node = classes.get(name)
        if node is None or name in seen:
            return set()
        names = _assigned_names(node)
        for base in _base_names(node):
            names |= declared(base, seen | {name})
        return names

    gaps = []
    for class_name, node in classes.items():
        # A mixin carries behaviour, not an engine identity, even when it
        # names an ``engine`` for the specs that consume it.
        if class_name.endswith("Mixin") or not _is_engine_spec(node):
            continue
        available = declared(class_name)
        missing = [attr for attr in REQUIRED if attr not in available]
        if missing:
            gaps.append((owners[class_name], class_name, missing))
    return sorted(gaps)


@register("engine_spec_metadata")
def detect(repo: Path) -> Iterator[Finding]:
    by_module: dict[str, list[tuple[str, list[str]]]] = {}
    for module, cls, missing in scan_repo(repo):
        by_module.setdefault(module, []).append((cls, missing))

    # One finding per module, not one for all of them: the module name is a
    # stable fingerprint, and a per-module PR is small enough that a reviewer
    # who knows that one dialect can judge it.
    for module, entries in sorted(by_module.items()):
        rows = ["| class | missing |", "| --- | --- |"]
        rows.extend(
            f"| `{cls}` | {', '.join(f'`{a}`' for a in missing)} |"
            for cls, missing in entries
        )
        engine = module.removesuffix(".py")
        yield Finding(
            detector="engine_spec_metadata",
            key=f"module:{module}",
            title=(
                f"chore(db-engines): {engine} engine spec is missing "
                f"user-facing metadata"
            ),
            severity=Severity.MODERATE,
            wave="B",
            labels=["devin-fix", "db-engines"],
            body=(
                "A concrete `BaseEngineSpec` subclass should declare `engine_name` "
                "(the label shown in the database-connection modal and the key the "
                "OAuth2 config is looked up under) and `sqlalchemy_uri_placeholder` "
                "(the example URI shown while the user types). Where a spec declares "
                "neither, the modal falls back to the generic "
                "`engine+driver://user:password@host:port/dbname`, which tells the "
                "user nothing about this dialect.\n\n"
                f"In `{SPEC_DIR}/{module}`:\n\n" + "\n".join(rows) + "\n\n"
                "Take the display name and URI shape from the dialect's own "
                "documentation. Do not invent connection parameters — if the correct "
                "URI shape is not documented, say so and stop."
            ),
            reproduce=(
                "python -m devin_pipeline.pipeline.cli --dry-run detect "
                "--only engine_spec_metadata"
            ),
            acceptance=[
                f"The detector no longer reports `{module}`",
                "Each placeholder URI matches the dialect's documented format",
                "`pytest tests/unit_tests/db_engine_specs -q` passes",
            ],
            metadata={
                "module": module,
                "specs": [
                    {"class": cls, "missing": missing} for cls, missing in entries
                ],
            },
            upstream_issue=42980,
        )
