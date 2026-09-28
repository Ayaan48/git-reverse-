#!/usr/bin/env python3
"""Seed the Hindsight bank with realistic synthetic CI/CD incidents.

    python scripts/seed_memory.py            # create bank if needed, then seed
    python scripts/seed_memory.py --wait     # block until each is searchable
    python scripts/seed_memory.py --dry-run  # print the incidents, send nothing

Covers the main failure families the agent handles: missing imports, syntax
errors, dependency conflicts, flaky tests, GitHub Actions degradation, rate
limits, and runner capacity. Timestamps are spread over the last 60 days so
Hindsight's temporal retrieval has something to work with.

Every incident uses the same record shape the live pipeline retains, so seeded
and real memories are indistinguishable to recall. Re-running is idempotent:
each incident has a stable document id, so it is replaced rather than duplicated.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from healing_agent.config import load_env_file  # noqa: E402
from healing_agent.memory import HindsightService, format_incident  # noqa: E402


def _repair(description: str, file: str, tier: str, passed: bool) -> dict[str, Any]:
    return {"description": description, "file": file, "tier": tier,
            "passed_validation": passed}


def _gates(*rounds: tuple[bool, list[str]]) -> list[dict[str, Any]]:
    return [
        {"round": i, "passed": passed, "failed_stages": failed}
        for i, (passed, failed) in enumerate(rounds, start=1)
    ]


PASS = (True, [])

INCIDENTS: list[dict[str, Any]] = [
    # ------------------------------------------------------ missing imports --
    {
        "days_ago": 58, "repo": "acme/payments-api", "failure_class": "code",
        "confidence": 0.91, "recommended_action": "fix_code",
        "failure_types": ["import"], "packages": ["yaml", "PyYAML"],
        "error_signatures": [
            "CI/test at step 'Run pytest': failure",
            "ModuleNotFoundError: No module named 'yaml'",
            "E   File \"src/payments/config.py\", line 4, in <module> import yaml",
        ],
        "summary": "config.py imports yaml but PyYAML was never declared in "
                   "requirements.txt; it only worked locally because a dev "
                   "machine had it installed globally.",
        "repairs": [_repair("Declared PyYAML>=6.0 in requirements.txt",
                            "requirements.txt", "deterministic", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 51, "repo": "acme/web-dashboard", "failure_class": "code",
        "confidence": 0.88, "recommended_action": "fix_code",
        "failure_types": ["import"], "packages": ["markupsafe", "jinja2"],
        "error_signatures": [
            "ImportError: cannot import name 'soft_unicode' from 'markupsafe'",
            "File \".venv/lib/python3.11/site-packages/jinja2/filters.py\", line 13",
        ],
        "summary": "An unpinned markupsafe resolved to 2.1+, which removed "
                   "soft_unicode, breaking the pinned old Jinja2 2.11.",
        "repairs": [
            _repair("Removed the jinja2 import from templating.py", "app/templating.py",
                    "ai", False),
            _repair("Upgraded Jinja2 to >=3.1 so it no longer needs soft_unicode",
                    "requirements.txt", "ai", True),
        ],
        "validation": _gates((False, ["imports"]), PASS), "outcome": "succeeded",
        "notes": "Deleting the import was rejected: it removed the render_page "
                 "function's dependency and failed the symbol-preservation check.",
    },
    {
        "days_ago": 33, "repo": "acme/ml-pipeline", "failure_class": "code",
        "confidence": 0.86, "recommended_action": "fix_code",
        "failure_types": ["import", "type"], "packages": ["typing"],
        "error_signatures": [
            "F821 undefined name 'Optional' [pipeline/features.py:42]",
            "NameError: name 'Optional' is not defined",
        ],
        "summary": "A refactor removed `from typing import Optional` while "
                   "annotations still used it.",
        "repairs": [_repair("Added `from typing import Optional`",
                            "pipeline/features.py", "deterministic", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    # ------------------------------------------------------- syntax errors --
    {
        "days_ago": 55, "repo": "acme/payments-api", "failure_class": "code",
        "confidence": 0.95, "recommended_action": "fix_code",
        "failure_types": ["syntax"], "packages": [],
        "error_signatures": [
            "SyntaxError: '(' was never closed [src/payments/routes.py:88]",
            "E999 SyntaxError reported by ruff",
        ],
        "summary": "A merge conflict resolution dropped a closing parenthesis "
                   "in a multi-line call.",
        "repairs": [_repair("Closed the parenthesis on the charge() call",
                            "src/payments/routes.py", "ai", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 44, "repo": "acme/infra-tools", "failure_class": "code",
        "confidence": 0.93, "recommended_action": "fix_code",
        "failure_types": ["indentation", "syntax"], "packages": [],
        "error_signatures": [
            "TabError: inconsistent use of tabs and spaces in indentation "
            "[tools/rotate_keys.py:17]",
        ],
        "summary": "An editor with tabs enabled saved a mixed-indentation file.",
        "repairs": [_repair("Converted tab indentation to 4 spaces",
                            "tools/rotate_keys.py", "deterministic", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 12, "repo": "acme/web-dashboard", "failure_class": "code",
        "confidence": 0.9, "recommended_action": "fix_code",
        "failure_types": ["syntax"], "packages": [],
        "error_signatures": [
            "SyntaxError: f-string: unmatched '[' [app/reports.py:61]",
            "Runs on python 3.11; the nested-quote f-string is only valid on 3.12+",
        ],
        "summary": "A PEP 701 f-string (nested same-type quotes) was written on "
                   "a 3.12 laptop but CI runs Python 3.11.",
        "repairs": [
            _repair("Rewrote report builder without the f-string",
                    "app/reports.py", "ai", False),
            _repair("Switched the inner quotes to single quotes, keeping the "
                    "f-string", "app/reports.py", "ai", True),
        ],
        "validation": _gates((False, ["syntax"]), PASS), "outcome": "succeeded",
        "notes": "The first rewrite was rejected for deleting the "
                 "build_summary function.",
    },
    # ---------------------------------------------- dependency conflicts --
    {
        "days_ago": 47, "repo": "acme/payments-api", "failure_class": "code",
        "confidence": 0.84, "recommended_action": "fix_code",
        "failure_types": ["dependency", "config"], "packages": ["pydantic", "fastapi"],
        "error_signatures": [
            "ERROR: ResolutionImpossible: fastapi 0.95.2 depends on "
            "pydantic!=1.7,<2.0.0,>=1.6.2",
            "The user requested pydantic>=2.5",
        ],
        "summary": "pydantic was bumped to v2 without bumping fastapi, which "
                   "still capped pydantic below 2.0.",
        "repairs": [_repair("Raised fastapi to >=0.110 (pydantic-2 compatible)",
                            "requirements.txt", "ai", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 29, "repo": "acme/ml-pipeline", "failure_class": "code",
        "confidence": 0.8, "recommended_action": "fix_code",
        "failure_types": ["dependency"], "packages": ["numpy", "pandas"],
        "error_signatures": [
            "ValueError: numpy.dtype size changed, may indicate binary "
            "incompatibility. Expected 96 from C header, got 88 from PyObject",
            "import pandas -> pandas/_libs/interval.pyx",
        ],
        "summary": "numpy 2.0 was pulled in while pandas was pinned to a build "
                   "compiled against the numpy 1.x ABI.",
        "repairs": [_repair("Pinned numpy<2 until pandas is upgraded",
                            "requirements.txt", "deterministic", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 18, "repo": "acme/web-dashboard", "failure_class": "code",
        "confidence": 0.82, "recommended_action": "fix_code",
        "failure_types": ["dependency", "config"], "packages": ["react", "react-dom"],
        "error_signatures": [
            "npm ERR! code ERESOLVE",
            "npm ERR! peer react@\"^17.0.0\" from react-dom@17.0.2",
            "npm ERR! Found: react@18.3.1",
        ],
        "summary": "react was upgraded to 18 but react-dom stayed on 17.",
        "repairs": [
            _repair("Added --legacy-peer-deps to the install step",
                    ".github/workflows/ci.yml", "ai", False),
            _repair("Upgraded react-dom to ^18.3.1 to match react",
                    "package.json", "ai", True),
        ],
        "validation": _gates((False, ["tests"]), PASS), "outcome": "succeeded",
        "notes": "--legacy-peer-deps was rejected: it hid the conflict and the "
                 "render tests then failed at runtime.",
    },
    # -------------------------------------------------------- flaky tests --
    {
        "days_ago": 40, "repo": "acme/payments-api", "failure_class": "code",
        "confidence": 0.62, "recommended_action": "fix_code",
        "failure_types": ["test"], "packages": ["pytest-asyncio"],
        "error_signatures": [
            "FAILED tests/test_webhooks.py::test_retry_backoff - "
            "AssertionError: assert 2 == 3",
            "Passes on re-run; fails ~1 in 6 runs",
        ],
        "summary": "The test asserted on wall-clock sleeps; slow shared runners "
                   "made the retry count nondeterministic.",
        "repairs": [
            _repair("Marked test_retry_backoff with @pytest.mark.skip",
                    "tests/test_webhooks.py", "ai", False),
            _repair("Injected a fake clock so retries are counted "
                    "deterministically", "tests/test_webhooks.py", "ai", True),
        ],
        "validation": _gates((False, ["tests"]), PASS), "outcome": "succeeded",
        "notes": "Skipping the test was rejected by policy: never skip tests to "
                 "make a build pass.",
    },
    {
        "days_ago": 22, "repo": "acme/ml-pipeline", "failure_class": "code",
        "confidence": 0.58, "recommended_action": "fix_code",
        "failure_types": ["test"], "packages": [],
        "error_signatures": [
            "FAILED tests/test_cache.py::test_eviction - FileExistsError: "
            "[Errno 17] File exists: '/tmp/feature-cache'",
            "Only fails when tests run in a different order (pytest-randomly)",
        ],
        "summary": "Two tests shared a hard-coded /tmp directory, so the result "
                   "depended on test order.",
        "repairs": [_repair("Switched the cache fixture to pytest's tmp_path",
                            "tests/conftest.py", "ai", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    # ---------------------------------- GitHub Actions platform degradation --
    {
        "days_ago": 53, "repo": "acme/infra-tools", "failure_class": "platform",
        "confidence": 0.89, "recommended_action": "retry_with_backoff",
        "failure_types": ["capacity", "service"], "packages": [],
        "error_signatures": [
            "The hosted runner: GitHub Actions 14 lost communication with the server.",
            "githubstatus.com: Actions - degraded performance",
            "4 unrelated workflows failing at once",
        ],
        "summary": "GitHub Actions incident: hosted runners dropped mid-job "
                   "across unrelated workflows. No code change involved.",
        "repairs": [], "remediations": ["retry_with_backoff - re-ran after the "
                                        "incident cleared (succeeded)"],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 36, "repo": "acme/web-dashboard", "failure_class": "platform",
        "confidence": 0.85, "recommended_action": "retry_with_backoff",
        "failure_types": ["network", "service"], "packages": [],
        "error_signatures": [
            "Error: failed to download action 'actions/checkout@v4'",
            "Response status code does not indicate success: 502 (Bad Gateway)",
        ],
        "summary": "The actions marketplace/CDN returned 5xx; the job never "
                   "reached user code.",
        "repairs": [], "remediations": ["retry_with_backoff - succeeded on the "
                                        "second attempt"],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 9, "repo": "acme/payments-api", "failure_class": "platform",
        "confidence": 0.9, "recommended_action": "retry_with_backoff",
        "failure_types": ["auth"], "packages": [],
        "error_signatures": [
            "remote: Your account is suspended. Please visit "
            "https://support.github.com",
            "fatal: unable to access 'https://github.com/acme/payments-api/': "
            "The requested URL returned error: 403",
            "githubstatus.com: Git Operations - partial outage",
        ],
        "summary": "Misleading 'account suspended' during a GitHub auth-service "
                   "incident. The account was healthy.",
        "repairs": [], "remediations": ["retry_with_backoff - succeeded once "
                                        "the incident was resolved"],
        "validation": _gates(PASS), "outcome": "succeeded",
        "notes": "Rotating the token did not help; it was not a credential problem.",
    },
    # ------------------------------------------------------- rate limits --
    {
        "days_ago": 49, "repo": "acme/infra-tools", "failure_class": "platform",
        "confidence": 0.92, "recommended_action": "retry_with_backoff",
        "failure_types": ["rate_limit"], "packages": [],
        "error_signatures": [
            "HttpError: API rate limit exceeded for installation ID 3141592",
            "x-ratelimit-remaining: 0",
        ],
        "summary": "A matrix of 40 jobs all called the GitHub API at once and "
                   "exhausted the installation rate limit.",
        "repairs": [], "remediations": [
            "retry_with_backoff (immediate retry) - failed, deepened the limit",
            "retry_with_backoff (jittered, after reset) - succeeded",
        ],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 25, "repo": "acme/ml-pipeline", "failure_class": "platform",
        "confidence": 0.87, "recommended_action": "retry_with_backoff",
        "failure_types": ["rate_limit"], "packages": [],
        "error_signatures": [
            "toomanyrequests: You have reached your pull rate limit. You may "
            "increase the limit by authenticating and upgrading",
            "docker pull python:3.11-slim",
        ],
        "summary": "Anonymous Docker Hub pulls from shared runner IPs hit the "
                   "pull rate limit.",
        "repairs": [_repair("Added docker/login-action before the pull so pulls "
                            "are authenticated", ".github/workflows/build.yml",
                            "ai", True)],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    # ---------------------------------------------------- runner capacity --
    {
        "days_ago": 31, "repo": "acme/ml-pipeline", "failure_class": "platform",
        "confidence": 0.9, "recommended_action": "failover_runner",
        "failure_types": ["capacity"], "packages": [],
        "error_signatures": [
            "No runner matching the specified labels was found: gpu-large",
            "Job queued for 45m",
        ],
        "summary": "The self-hosted GPU pool was scaled to zero; jobs could not "
                   "be assigned.",
        "repairs": [_repair("Parameterised runs-on via vars.GPU_RUNNER so the pool "
                            "can be switched without a code change",
                            ".github/workflows/train.yml", "deterministic", True)],
        "remediations": ["failover_runner - switched to the gpu-medium pool"],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    {
        "days_ago": 4, "repo": "acme/web-dashboard", "failure_class": "platform",
        "confidence": 0.83, "recommended_action": "failover_runner",
        "failure_types": ["capacity"], "packages": [],
        "error_signatures": [
            "Waiting for a runner to pick up this job...",
            "3 runs stuck in queue past 10 minutes; queue p95 1900s",
        ],
        "summary": "Hosted ubuntu-latest capacity was exhausted during a regional "
                   "incident.",
        "repairs": [_repair("Parameterised runs-on so the pool can fail over",
                            ".github/workflows/ci.yml", "deterministic", True)],
        "remediations": ["failover_runner - moved to ubuntu-22.04 larger runner"],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
    # --------------------------------------------------------------- mixed --
    {
        "days_ago": 15, "repo": "acme/payments-api", "failure_class": "mixed",
        "confidence": 0.7, "recommended_action": "fix_code",
        "failure_types": ["syntax", "service"], "packages": [],
        "error_signatures": [
            "IndentationError: unexpected indent [src/payments/refunds.py:23]",
            "githubstatus.com: Actions - degraded performance",
        ],
        "summary": "A real indentation error landed during an Actions incident. "
                   "Retrying alone would not have fixed it; the code was repaired "
                   "and the pipeline re-run after the incident.",
        "repairs": [_repair("Fixed the indentation of the refund loop",
                            "src/payments/refunds.py", "deterministic", True)],
        "remediations": ["retry_with_backoff - re-ran once Actions recovered"],
        "validation": _gates(PASS), "outcome": "succeeded",
    },
]


def build_records(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    records = []
    for index, incident in enumerate(INCIDENTS, start=1):
        record = {k: v for k, v in incident.items() if k != "days_ago"}
        # Spread within the day too, so incidents never share a timestamp.
        stamp = now - timedelta(days=incident["days_ago"], hours=(index * 5) % 24)
        record["timestamp"] = stamp.isoformat()
        record["incident_id"] = f"seed-{index:03d}"
        records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wait", action="store_true",
                        help="block until each incident is processed")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the incidents without sending anything")
    parser.add_argument("--skip-bank-setup", action="store_true")
    args = parser.parse_args()

    records = build_records()
    if args.dry_run:
        for record in records:
            print(format_incident(record), end="\n\n")
        print(f"{len(records)} incident(s) (dry run, nothing sent)")
        return 0

    load_env_file(ROOT / ".env")
    service = HindsightService(timeout=120.0)
    if not service.enabled:
        print(f"Memory disabled: {service.disabled_reason}. Set it in .env first.")
        return 1

    if not args.skip_bank_setup:
        result = service.ensure_bank()
        print(f"Bank '{result['bank_id']}' ready")

    ok = 0
    for record in records:
        retained = service.retain(record, wait=args.wait)
        ok += retained
        mark = "ok  " if retained else "FAIL"
        print(f"[{mark}] {record['incident_id']} {record['timestamp'][:10]} "
              f"{record['repo']:<20} {record['failure_class']:<8} "
              f"{', '.join(record['failure_types'])}")

    print(f"\nRetained {ok}/{len(records)} incident(s) into '{service.bank_id}'.")
    if not args.wait:
        print("Fact extraction runs server-side; allow a minute or two before "
              "recall returns them.")
    return 0 if ok == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
