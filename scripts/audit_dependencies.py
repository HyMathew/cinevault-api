"""Audit each pinned dependency set; preserve failures and never ignore advisories."""

import json
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    filenames = ("requirements.txt", "requirements-dev.txt", "requirements-quality.txt")
    results = [
        {"requirements": name, "exit_code": -1, "error": "Not executed"} for name in filenames
    ]
    summary = reports / "dependency-summary.json"
    summary.write_text(json.dumps(results, indent=2), encoding="utf-8")
    for index, filename in enumerate(filenames):
        name = filename.removesuffix(".txt")
        output = reports / (name + "-audit.json")
        log = reports / (name + "-audit.log")
        output.write_text("null\n", encoding="utf-8")
        log.write_text("Attempt started\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "-r",
                    filename,
                    "--no-deps",
                    "--disable-pip",
                    "--strict",
                    "-f",
                    "json",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
            )
            output.write_text(result.stdout, encoding="utf-8")
            log.write_text(result.stderr, encoding="utf-8")
            results[index] = {"requirements": filename, "exit_code": result.returncode}
        except (OSError, subprocess.TimeoutExpired) as exc:
            results[index] = {
                "requirements": filename,
                "exit_code": -1,
                "error": type(exc).__name__,
            }
            log.write_text(type(exc).__name__ + "\n", encoding="utf-8")
        finally:
            summary.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(filename, "PASS" if results[index]["exit_code"] == 0 else "FAIL", flush=True)
    raise SystemExit(any(item["exit_code"] for item in results))


if __name__ == "__main__":
    main()
