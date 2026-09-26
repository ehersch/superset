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

from devin_pipeline.pipeline.models import Evidence, Finding, Severity


def make_finding(**overrides: object) -> Finding:
    kwargs: dict[str, object] = {
        "detector": "npm_audit",
        "key": "lerna@8.2.4",
        "title": "security(frontend): upgrade lerna",
        "severity": Severity.CRITICAL,
        "body": "body text",
        "wave": "A",
        "reproduce": "npm audit --package-lock-only",
        "acceptance": ["no advisories remain", "build passes"],
        "labels": ["devin-fix"],
    }
    kwargs.update(overrides)
    return Finding(**kwargs)  # type: ignore[arg-type]


def test_fingerprint_is_stable_and_keyed_on_detector_and_key() -> None:
    assert make_finding().fingerprint == make_finding(title="different").fingerprint
    assert make_finding().fingerprint != make_finding(key="other@1.0.0").fingerprint
    assert make_finding().fingerprint != make_finding(detector="osv_python").fingerprint


def test_issue_body_round_trips_through_evidence() -> None:
    finding = make_finding(upstream_issue=44436, metadata={"packages": ["lerna"]})
    evidence = Evidence.parse(finding.issue_body())
    assert evidence is not None
    assert evidence.fingerprint == finding.fingerprint
    assert evidence.detector == "npm_audit"
    assert evidence.reproduce == finding.reproduce
    assert evidence.acceptance == finding.acceptance
    assert evidence.upstream_issue == 44436
    assert evidence.metadata == {"packages": ["lerna"]}


def test_issue_body_renders_acceptance_as_a_checklist() -> None:
    body = make_finding().issue_body()
    assert "- [ ] no advisories remain" in body
    assert "- [ ] build passes" in body


def test_parse_returns_none_without_a_usable_block() -> None:
    assert Evidence.parse("a hand-written issue with no evidence") is None
    assert Evidence.parse("<!-- devin-pipeline:evidence\nnot json\n-->") is None
