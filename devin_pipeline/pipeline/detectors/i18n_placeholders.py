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
"""Detect translations whose placeholders disagree with the English source.

A catalog entry that drops ``%(name)s`` does not merely look wrong: the backend
interpolates with ``%``, so a missing or renamed placeholder raises at runtime
and turns a localized page into a 500. The check is exact and cheap, which
makes it both a detector and the verifier the fix has to satisfy.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Iterator

from ..models import Finding, Severity
from .base import register

CATALOG_GLOB = "superset/translations/*/LC_MESSAGES/messages.po"
ENTRY = re.compile(
    r'msgid\s+((?:"(?:[^"\\]|\\.)*"\s*)+)msgstr\s+((?:"(?:[^"\\]|\\.)*"\s*)+)'
)
STRING_PART = re.compile(r'"((?:[^"\\]|\\.)*)"')
NAMED = re.compile(r"%\((\w+)\)[sdfr]")
POSITIONAL = re.compile(r"%[sdfr]")
BRACED = re.compile(r"\{(\w*)\}")
# A locale with this many broken entries gets its own issue; the rest are
# grouped so the pipeline files a handful of reviewable PRs, not twenty.
OWN_ISSUE_THRESHOLD = 60


def _join(block: str) -> str:
    return "".join(STRING_PART.findall(block))


def _signature(text: str) -> tuple[frozenset[str], int, frozenset[str]]:
    return (
        frozenset(NAMED.findall(text)),
        len(POSITIONAL.findall(text)),
        frozenset(BRACED.findall(text)),
    )


def scan_catalog(path: Path) -> list[tuple[str, str]]:
    """Return ``(msgid, msgstr)`` pairs whose placeholder sets disagree."""
    broken = []
    for msgid_block, msgstr_block in ENTRY.findall(path.read_text(encoding="utf-8")):
        msgid, msgstr = _join(msgid_block), _join(msgstr_block)
        if not msgid or not msgstr:
            continue
        if _signature(msgid) != _signature(msgstr):
            broken.append((msgid, msgstr))
    return broken


def scan_repo(repo: Path) -> dict[str, list[tuple[str, str]]]:
    results = {}
    for path in sorted(repo.glob(CATALOG_GLOB)):
        locale = path.parts[-3]
        broken = scan_catalog(path)
        if broken:
            results[locale] = broken
    return results


def _sample(entries: list[tuple[str, str]], limit: int = 5) -> str:
    rows = ["| msgid | msgstr |", "| --- | --- |"]
    for msgid, msgstr in entries[:limit]:
        rows.append(f"| `{msgid[:70]}` | `{msgstr[:70]}` |")
    return "\n".join(rows)


@register("i18n_placeholders")
def detect(repo: Path) -> Iterator[Finding]:
    results = scan_repo(repo)
    if not results:
        return
    counts = Counter({locale: len(entries) for locale, entries in results.items()})

    grouped: list[str] = []
    for locale, count in counts.most_common():
        if count >= OWN_ISSUE_THRESHOLD:
            entries = results[locale]
            yield Finding(
                detector="i18n_placeholders",
                key=f"locale:{locale}",
                title=(
                    f"i18n({locale}): {count} translations have placeholders "
                    f"that do not match the source"
                ),
                severity=Severity.HIGH,
                wave="B",
                labels=["devin-fix", "i18n"],
                body=(
                    f"`superset/translations/{locale}/LC_MESSAGES/messages.po` "
                    f"contains **{count}** entries whose placeholder set differs "
                    f"from the English "
                    f"msgid. Entries that drop or rename a named placeholder raise at "
                    f"interpolation time on the backend and render a raw template or a "
                    f"blank value on the frontend.\n\n"
                    f"### Sample\n\n{_sample(entries)}\n\n"
                    f"Clear (do not guess at) any translation whose placeholders "
                    f"cannot be repaired faithfully — falling back to English is "
                    f"strictly better than a 500."
                ),
                reproduce=(
                    "python -m devin_pipeline.pipeline.cli --dry-run detect "
                    "--only i18n_placeholders"
                ),
                acceptance=[
                    f"The detector reports 0 mismatches for `{locale}`",
                    "`messages.mo` is regenerated for the locale",
                    "No msgid text is modified — only translations",
                ],
                metadata={"locale": locale, "count": count},
            )
        else:
            grouped.append(locale)

    if grouped:
        total = sum(counts[locale] for locale in grouped)
        table = "\n".join(f"- `{locale}`: {counts[locale]}" for locale in grouped)
        yield Finding(
            detector="i18n_placeholders",
            key="locale:tail",
            title=(
                f"i18n: {total} broken placeholders across "
                f"{len(grouped)} smaller locales"
            ),
            severity=Severity.MODERATE,
            wave="B",
            labels=["devin-fix", "i18n"],
            body=(
                "The remaining locales each carry a small number of entries whose "
                "placeholders disagree with the English source:\n\n"
                f"{table}\n\n"
                "Same rule as the per-locale issues: repair faithfully, or clear the "
                "entry so it falls back to English."
            ),
            reproduce=(
                "python -m devin_pipeline.pipeline.cli --dry-run detect "
                "--only i18n_placeholders"
            ),
            acceptance=[
                "The detector reports 0 mismatches for every locale listed above",
                "`messages.mo` is regenerated for each touched locale",
                "No msgid text is modified — only translations",
            ],
            metadata={"locales": grouped, "count": total},
        )
