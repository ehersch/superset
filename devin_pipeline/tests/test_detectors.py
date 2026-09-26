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
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from devin_pipeline.pipeline.detectors import (
    base,
    engine_spec_metadata,
    i18n_placeholders,
    npm_audit,
)
from devin_pipeline.pipeline.models import Severity

CATALOG = """\
msgid ""
msgstr ""
"Content-Type: text/plain\\n"

msgid "Deleted %(num)d dashboard"
msgstr "Suppression de tableaux"

msgid "Added %(name)s to %(target)s"
msgstr "Ajout de %(name)s a %(target)s"

msgid "Untranslated"
msgstr ""
"""


def write_catalog(tmp_path: Path, locale: str, text: str = CATALOG) -> Path:
    path = (
        tmp_path / "superset" / "translations" / locale / "LC_MESSAGES" / "messages.po"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_i18n_flags_only_entries_whose_placeholders_differ(tmp_path: Path) -> None:
    path = write_catalog(tmp_path, "fr")
    broken = i18n_placeholders.scan_catalog(path)
    assert [msgid for msgid, _ in broken] == ["Deleted %(num)d dashboard"]


def test_i18n_groups_small_locales_into_one_finding(tmp_path: Path) -> None:
    write_catalog(tmp_path, "fr")
    write_catalog(tmp_path, "de")
    findings = list(i18n_placeholders.detect(tmp_path))
    assert len(findings) == 1
    assert findings[0].key == "locale:tail"
    assert set(findings[0].metadata["locales"]) == {"fr", "de"}


def test_i18n_gives_a_heavy_locale_its_own_finding(tmp_path: Path) -> None:
    entries = "\n".join(
        f'msgid "item {i} %(name)s"\nmsgstr "item {i}"\n' for i in range(70)
    )
    write_catalog(tmp_path, "ko", entries)
    findings = list(i18n_placeholders.detect(tmp_path))
    assert [f.key for f in findings] == ["locale:ko"]
    assert findings[0].metadata["count"] == 70


ENGINE_SPEC = """\
class CompleteSpec(BaseEngineSpec):
    engine = "complete"
    engine_name = "Complete"
    sqlalchemy_uri_placeholder = "complete://user@host/db"


class PartialSpec(BaseEngineSpec):
    engine = "partial"
    engine_name = "Partial"


class NotASpec:
    engine_name = "irrelevant"
"""


def test_engine_spec_detector_reports_only_incomplete_concrete_specs(
    tmp_path: Path,
) -> None:
    spec_dir = tmp_path / "superset" / "db_engine_specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "sample.py").write_text(ENGINE_SPEC, encoding="utf-8")
    (spec_dir / "base.py").write_text("class BaseEngineSpec:\n    engine = 'base'\n")
    gaps = engine_spec_metadata.scan_repo(tmp_path)
    assert gaps == [("sample.py", "PartialSpec", ["sqlalchemy_uri_placeholder"])]


AUDIT = {
    "vulnerabilities": {
        "underscore": {
            "severity": "critical",
            "fixAvailable": {"name": "po2json", "version": "1.0.0"},
            "via": [{"title": "Arbitrary code execution", "url": "https://example/1"}],
        },
        "nomnom": {
            "severity": "high",
            "fixAvailable": {"name": "po2json", "version": "1.0.0"},
            "via": [{"title": "Prototype pollution", "url": "https://example/2"}],
        },
        "unfixable": {"severity": "high", "fixAvailable": False, "via": []},
    }
}


def test_npm_audit_groups_by_fix_target_and_skips_unfixable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = tmp_path / "superset-frontend"
    frontend.mkdir()
    (frontend / "package-lock.json").write_text("{}")
    monkeypatch.setattr(
        npm_audit,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(AUDIT), stderr=""
        ),
    )
    findings = list(npm_audit.detect(tmp_path))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.key == "po2json@1.0.0"
    assert finding.severity is Severity.CRITICAL
    assert finding.metadata["packages"] == ["nomnom", "underscore"]


def test_registry_exposes_every_detector() -> None:
    assert set(base.registry()) == {
        "npm_audit",
        "osv_python",
        "i18n_placeholders",
        "engine_spec_metadata",
        "upstream_mirror",
    }


def test_every_finding_carries_a_reproduction_and_acceptance_criteria() -> None:
    from devin_pipeline.pipeline.detectors import upstream_mirror

    findings = list(upstream_mirror.detect(Path(".")))
    assert findings
    for finding in findings:
        assert finding.reproduce.strip()
        assert finding.acceptance
        assert "devin-fix" in finding.labels
