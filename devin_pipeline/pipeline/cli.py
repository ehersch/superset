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
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    detect.add_argument("--html", help="write the findings as an HTML page")
    detect.add_argument(
        "--serve",
        nargs="?",
        type=int,
        const=8000,
        help="serve that page on this port instead of printing (default 8000)",
    )

    file_cmd = sub.add_parser("file", help="run detectors and file/refresh issues")
    file_cmd.add_argument("--only", nargs="*")

    approve = sub.add_parser(
        "approve",
        help="label unclaimed detector issues devin-fix under the standing policy",
    )
    approve.add_argument(
        "--limit", type=int, help="issues to approve this run (AUTO_APPROVE_LIMIT)"
    )
    approve.add_argument(
        "--min-severity",
        choices=["critical", "high", "moderate", "low"],
        help="least severe finding to approve (AUTO_APPROVE_MIN_SEVERITY)",
    )

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

    metrics = sub.add_parser("metrics", help="print pipeline metrics as JSON")
    metrics.add_argument("--out", help="file to write the metrics JSON to")

    dash = sub.add_parser("dashboard", help="render the HTML status dashboard")
    dash.add_argument("--out", default="dashboard.html")
    dash.add_argument(
        "--serve",
        nargs="?",
        type=int,
        const=8000,
        help="also serve it on this port (default 8000)",
    )

    run = sub.add_parser(
        "run", help="detect, file, dispatch and poll to completion in one process"
    )
    run.add_argument("--only", nargs="*", help="limit to these detectors")
    run.add_argument(
        "--approve",
        type=int,
        metavar="N",
        help="auto-approve up to N unclaimed issues before dispatching",
    )
    run.add_argument("--poll-interval", type=int, default=60, help="seconds")
    run.add_argument("--timeout", type=int, default=3600, help="seconds")
    run.add_argument("--dashboard-out", help="write the HTML dashboard here when done")
    return parser


def _serve(html: str, port: int) -> int:
    """Serve one page until interrupted, so `docker run -p` can show it."""
    payload = html.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's dispatch name)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            return

    print(f"serving findings on http://localhost:{port} (ctrl-c to stop)")
    # An empty host binds every interface, which is what `docker run -p` needs
    # to reach the server from the host.
    with ThreadingHTTPServer(("", port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def _run_detect(
    pipeline: Pipeline,
    only: list[str] | None,
    as_json: bool,
    html: str | None = None,
    serve: int | None = None,
) -> int:
    findings = pipeline.detect(only)
    if html or serve:
        page = pipeline.findings_page(findings)
        if html:
            Path(html).write_text(page, encoding="utf-8")
            print(f"wrote {html}")
        return _serve(page, serve) if serve else 0
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


def _run_approve(pipeline: Pipeline) -> int:
    approved = pipeline.auto_approve()
    print(f"approved {len(approved)} issue(s): {' '.join(f'#{n}' for n in approved)}")
    return 0


def _run_dispatch(pipeline: Pipeline, issue: int | None) -> int:
    if issue:
        attempt = pipeline.dispatch_issue(issue)
        print(f"session: {attempt.session_id if attempt else 'not dispatched'}")
    else:
        attempts = pipeline.dispatch_labelled()
        print(f"dispatched {len(attempts)} session(s)")
    return 0


def _emit(text: str, out: str | None) -> int:
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def _run_dashboard(pipeline: Pipeline, out: str, serve: int | None = None) -> int:
    page = pipeline.dashboard()
    Path(out).write_text(page, encoding="utf-8")
    print(f"wrote {out}")
    return _serve(page, serve) if serve else 0


def _run_file(pipeline: Pipeline, only: list[str] | None) -> int:
    filed = pipeline.file_issues(pipeline.detect(only))
    print(f"filed {len(filed)} new issue(s)")
    return 0


def _run_monitor(pipeline: Pipeline) -> int:
    settled = pipeline.monitor()
    print(f"settled {len(settled)} session(s)")
    return 0


def _run_ci_failure(pipeline: Pipeline, pr_url: str, head_sha: str) -> int:
    handled = pipeline.handle_ci_failure(pr_url, head_sha)
    print("fed back to session" if handled else "not handled")
    return 0


def _run_loop(
    pipeline: Pipeline,
    only: list[str] | None,
    poll_interval: int,
    timeout: int,
    dashboard_out: str | None,
) -> int:
    """The whole loop in one process: detect, file, dispatch, poll, report.

    The GitHub triggers run these phases as separate events; this runs them
    back to back so the automation can be demonstrated from a single container
    without a webhook receiver in front of it.
    """
    log = logging.getLogger("devin_pipeline.run")
    filed = pipeline.file_issues(pipeline.detect(only))
    log.info("filed %d new issue(s)", len(filed))

    approved = pipeline.auto_approve()
    log.info("auto-approved %d issue(s)", len(approved))

    attempts = pipeline.dispatch_labelled()
    for attempt in attempts:
        log.info("session %s -> %s", attempt.session_id, attempt.session_url)
    log.info("dispatched %d session(s)", len(attempts))

    deadline = time.monotonic() + timeout
    while pipeline.metrics()["in_flight"]:
        if time.monotonic() >= deadline:
            log.warning("timed out with sessions still in flight")
            break
        time.sleep(poll_interval)
        for record in pipeline.monitor():
            log.info("settled issue #%d", record.issue_number)

    print(json.dumps(pipeline.metrics(), indent=2))
    if dashboard_out:
        _run_dashboard(pipeline, dashboard_out)
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
    if args.command == "approve":
        if args.limit is not None:
            config.auto_approve_limit = args.limit
        if args.min_severity:
            config.auto_approve_min_severity = args.min_severity
    if args.command == "run" and args.approve is not None:
        config.auto_approve_limit = args.approve
    pipeline = Pipeline(config)

    handlers: dict[str, Callable[[], int]] = {
        "detect": lambda: _run_detect(
            pipeline, args.only, args.as_json, args.html, args.serve
        ),
        "file": lambda: _run_file(pipeline, args.only),
        "approve": lambda: _run_approve(pipeline),
        "dispatch": lambda: _run_dispatch(pipeline, args.issue),
        "monitor": lambda: _run_monitor(pipeline),
        "ci-failure": lambda: _run_ci_failure(pipeline, args.pr_url, args.head_sha),
        "report": lambda: _emit(pipeline.report(), args.out),
        "metrics": lambda: _emit(json.dumps(pipeline.metrics(), indent=2), args.out),
        "dashboard": lambda: _run_dashboard(pipeline, args.out, args.serve),
        "run": lambda: _run_loop(
            pipeline,
            args.only,
            args.poll_interval,
            args.timeout,
            args.dashboard_out,
        ),
    }
    handler = handlers.get(args.command)
    return handler() if handler else 1


if __name__ == "__main__":
    sys.exit(main())
