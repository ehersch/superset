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
"""Command line entrypoint.

Every trigger — the nightly schedule, the label event, the check-suite event —
invokes one of these subcommands, so the automation can be driven by hand,
from CI, or from a webhook receiver without a second implementation.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config
from .orchestrator import Pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devin-pipeline")
    parser.add_argument(
        "--dry-run", action="store_true", help="never write to GitHub or Devin"
    )
    parser.add_argument("--repo-path", help="path to the Superset checkout to scan")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", help="run detectors and print findings")
    detect.add_argument("--only", nargs="*", help="limit to these detectors")
    detect.add_argument("--json", action="store_true", dest="as_json")

    file_cmd = sub.add_parser("file", help="run detectors and file/refresh issues")
    file_cmd.add_argument("--only", nargs="*")

    dispatch = sub.add_parser("dispatch", help="start sessions for devin-fix issues")
    dispatch.add_argument("--issue", type=int, help="dispatch a single issue")

    sub.add_parser("monitor", help="poll live sessions and settle finished ones")

    ci = sub.add_parser(
        "ci-failure", help="feed a failing check suite back to a session"
    )
    ci.add_argument("--pr-url", required=True)
    ci.add_argument("--head-sha", required=True)

    report = sub.add_parser("report", help="write a run report")
    report.add_argument("--out", help="file to write the markdown report to")
    return parser


def _run_detect(pipeline: Pipeline, only: list[str] | None, as_json: bool) -> int:
    findings = pipeline.detect(only)
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "fingerprint": f.fingerprint,
                        "detector": f.detector,
                        "key": f.key,
                        "severity": f.severity.value,
                        "wave": f.wave,
                        "title": f.title,
                    }
                    for f in findings
                ],
                indent=2,
            )
        )
    else:
        for finding in findings:
            severity = f"{finding.severity.value:8}"
            print(f"[{severity}] {finding.fingerprint}  {finding.title}")
        print(f"\n{len(findings)} finding(s)")
    return 0


def _run_dispatch(pipeline: Pipeline, issue: int | None) -> int:
    if issue:
        attempt = pipeline.dispatch_issue(issue)
        print(f"session: {attempt.session_id if attempt else 'not dispatched'}")
    else:
        attempts = pipeline.dispatch_labelled()
        print(f"dispatched {len(attempts)} session(s)")
    return 0


def _run_report(pipeline: Pipeline, out: str | None) -> int:
    text = pipeline.report()
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = Config()
    if args.dry_run:
        config.dry_run = True
    if args.repo_path:
        config.repo_path = Path(args.repo_path).resolve()
    pipeline = Pipeline(config)

    if args.command == "detect":
        return _run_detect(pipeline, args.only, args.as_json)

    if args.command == "file":
        filed = pipeline.file_issues(pipeline.detect(args.only))
        print(f"filed {len(filed)} new issue(s)")
        return 0

    if args.command == "dispatch":
        return _run_dispatch(pipeline, args.issue)

    if args.command == "monitor":
        settled = pipeline.monitor()
        print(f"settled {len(settled)} session(s)")
        return 0

    if args.command == "ci-failure":
        handled = pipeline.handle_ci_failure(args.pr_url, args.head_sha)
        print("fed back to session" if handled else "not handled")
        return 0

    if args.command == "report":
        return _run_report(pipeline, args.out)

    return 1


if __name__ == "__main__":
    sys.exit(main())
