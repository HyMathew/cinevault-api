"""Keep a bounded receipt; failed/shrinking runs are never labelled a full pass."""

import json
import os
import platform
import subprocess
from importlib.metadata import version
from pathlib import Path

from .metrics import COUNTS


def pytest_sessionfinish(session, exitstatus):
    root = Path(__file__).resolve().parents[2]
    report = Path(os.environ.get("PROPERTY_REPORT_PATH", root / "reports/property-testing.json"))
    report.parent.mkdir(parents=True, exist_ok=True)
    source = (
        os.environ.get("PROPERTY_SOURCE_HEAD")
        or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    )
    report.write_text(
        json.dumps(
            {
                "source_head": source,
                "source_may_include_working_changes": True,
                "python": platform.python_version(),
                "platform": platform.system(),
                "hypothesis": version("hypothesis"),
                "pytest_exitstatus": int(exitstatus),
                "campaigns": COUNTS,
                "native_mongodb_executed": False,
                "counts_include_successful_replays_when_running_failed_cases": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
