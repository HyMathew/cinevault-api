"""Named manual mutation experiments in an immutable Git export, never mutmut.

No fork/tool dependency is necessary. Each single textual patch has an explicit
contract assertion. This is a bounded experiment ledger, not a mutation score or
an automatic search over all program mutations. The source export is retained.
"""

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

CRITICAL = "tests/properties/test_critical_contract.py::"
CASES = (
    (
        "preflight-commits",
        "app/services/authentication.py",
        "return persistence.query(read)",
        "return persistence.run(read)",
        "test_preflight_is_readonly_even_when_authority_is_valid",
        "Preflight committed state",
    ),
    (
        "foreign-private-read",
        "app/persistence/json_repositories.py",
        'return item if item is not None and item["owner_id"] == owner_id else None',
        "return item if item is not None else None",
        "test_private_model_ownership_etag_and_audit_regression",
        "Private ownership boundary",
    ),
    (
        "stale-version-accepted",
        "app/services/catalogue.py",
        "if supplied != etag(entity):",
        "if False:",
        "test_private_model_ownership_etag_and_audit_regression",
        "ETag/precondition boundary",
    ),
    (
        "receipt-content-ignored",
        "app/services/receipts.py",
        'if cached["operation"] != operation or cached["request_hash"] != request_hash:',
        'if cached["operation"] != operation:',
        "test_receipt_model_replay_conflict_delete_and_ttl_regression",
        "Receipt content conflict",
    ),
    (
        "receipt-future-accepted",
        "app/services/receipts.py",
        'if cached["created_at"] > timestamp:',
        'if cached["created_at"] < timestamp:',
        "test_receipt_future_clock_and_cross_operation_are_rejected",
        "Future receipt must be rejected",
    ),
    (
        "receipt-expiry-inclusive",
        "app/persistence/json_repositories.py",
        'retained = [row for row in rows if row["expires_at"] > timestamp]',
        'retained = [row for row in rows if row["expires_at"] >= timestamp]',
        "test_receipt_model_replay_conflict_delete_and_ttl_regression",
        "New operation must not be a replay",
    ),
    (
        "absolute-expiry-inclusive",
        "app/services/authentication.py",
        'timestamp < session["expires_at"]',
        'timestamp <= session["expires_at"]',
        "test_absolute_expiry_idle_and_clock_rollback_are_separate_boundaries",
        "Session boundary: absolute",
    ),
    (
        "idle-expiry-inclusive",
        "app/services/authentication.py",
        'timestamp < session["last_seen"] + IDLE_TTL',
        'timestamp <= session["last_seen"] + IDLE_TTL',
        "test_absolute_expiry_idle_and_clock_rollback_are_separate_boundaries",
        "Session boundary: idle",
    ),
    (
        "clock-rollback-accepted",
        "app/services/authentication.py",
        'and timestamp >= session["last_seen"]',
        "and True",
        "test_absolute_expiry_idle_and_clock_rollback_are_separate_boundaries",
        "Session boundary: rollback",
    ),
    (
        "sixth-crypto-attempt",
        "app/services/authentication.py",
        'existing.get(key, {}).get("count", 0) >= MAX_ATTEMPTS',
        'existing.get(key, {}).get("count", 0) > MAX_ATTEMPTS',
        "test_sixth_failed_login_is_refused_before_the_crypto_provider",
        "Throttled request reached crypto provider",
    ),
    (
        "audit-retains-prefix",
        "app/persistence/json_repositories.py",
        "rows[:] = rows[-AUDIT_KEEP:]",
        "rows[:] = rows[:AUDIT_KEEP]",
        "test_audit_retains_the_last_append_even_when_its_clock_is_older",
        "Audit must retain append suffix",
    ),
    (
        "bson-small-integer-int32",
        "app/persistence/mongo_model.py",
        "return Int64(value) if encode else int(value)",
        "return int(value)",
        "tests/properties/test_bson_contract.py::test_int64_edges_and_ordinal_are_exact_not_bool",
        "BSON integer must be Int64",
    ),
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run_tests(export, output, name, selection, source):
    junit = output / (name + ".xml")
    started = time.monotonic()
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PROPERTY_SOURCE_HEAD": source,
        "PROPERTY_REPORT_PATH": str(output / (name + "-properties.json")),
    }
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                *selection,
                "-W",
                "error",
                "--junitxml=" + str(junit),
            ],
            cwd=export,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        log = (result.stdout + result.stderr).replace(str(export), "<isolated-export>")
        (output / (name + ".log")).write_text(log, encoding="utf-8")
        tests = ElementTree.parse(junit).getroot().findall(".//testcase") if junit.exists() else []
        failures = [item.find("failure") for item in tests if item.find("failure") is not None]
        errors = [item for item in tests if item.find("error") is not None]
        skipped = [item for item in tests if item.find("skipped") is not None]
        return {
            "exit_code": result.returncode,
            "tests": len(tests),
            "errors": len(errors),
            "skipped": len(skipped),
            "duration_seconds": round(time.monotonic() - started, 3),
        }, failures
    except subprocess.TimeoutExpired:
        (output / (name + ".log")).write_text(
            "Timed out; no detection claimed.\n", encoding="utf-8"
        )
        return {
            "exit_code": None,
            "status": "timeout",
            "duration_seconds": round(time.monotonic() - started, 3),
        }, []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    # Avoid accidentally exporting into production or an existing result folder.
    if output == root or root in output.parents:
        raise ValueError("Mutation output must be outside the active checkout")
    output.mkdir(parents=True, exist_ok=False)
    source = subprocess.check_output(
        ["git", "rev-parse", args.source_ref + "^{commit}"], cwd=root, text=True
    ).strip()
    archive = output / "source.zip"
    subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "archive",
            "--format=zip",
            "--output=" + str(archive),
            source,
        ],
        cwd=root,
        check=True,
    )
    export = output / "isolated-export"
    export.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = PurePosixPath(member.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or (member.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Unsafe export member")
        bundle.extractall(export)
    overlay = {}
    for path in sorted((root / "tests/properties").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        target = export / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        overlay[relative.as_posix()] = sha(path.read_bytes())
    report = {
        "production_source": source,
        "source_archive_sha256": sha(archive.read_bytes()),
        "test_overlay_sha256": overlay,
        "method": "explicit-single-patch-experiments",
        "mutmut_executed": False,
        "native_mongodb_executed": False,
        "baseline": None,
        "experiments": [],
    }
    receipt = output / "mutation-receipt.json"

    def persist():
        receipt.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    persist()
    baseline, _ = run_tests(export, output, "baseline", ["tests/properties"], source)
    report["baseline"] = baseline
    persist()
    if baseline["exit_code"] != 0 or baseline["errors"] or baseline["skipped"]:
        raise SystemExit("Mutation baseline failed; no mutant results claimed")
    for name, filename, old, new, test, marker in CASES:
        target = export / filename
        original = target.read_bytes()
        text = original.decode("utf-8")
        if text.count(old) != 1:
            report["experiments"].append({"name": name, "status": "anchor-mismatch"})
            persist()
            continue
        changed = text.replace(old, new)
        compile(changed, filename, "exec")
        patch = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                changed.splitlines(keepends=True),
                fromfile="a/" + filename,
                tofile="b/" + filename,
            )
        ).encode("utf-8")
        (output / (name + ".patch")).write_bytes(patch)
        selection = test if "::" in test else CRITICAL + test
        try:
            target.write_bytes(changed.encode("utf-8"))
            # Isolated source changes may have the same size/mtime. No stale pyc.
            for cached in target.parent.glob("__pycache__/" + target.stem + ".*.pyc"):
                cached.unlink()
            result, failures = run_tests(export, output, name, [selection], source)
            detected = (
                result["exit_code"] == 1
                and not result.get("errors")
                and not result.get("skipped")
                and any(
                    "AssertionError" in (item.text or "") and marker in (item.text or "")
                    for item in failures
                )
            )
            status = (
                "detected-by-behavioral-assertion"
                if detected
                else "survived"
                if result["exit_code"] == 0
                else result.get("status", "unclassified-test-or-tool-failure")
            )
            report["experiments"].append(
                {
                    "name": name,
                    "file": filename,
                    "original_sha256": sha(original),
                    "mutated_sha256": sha(changed.encode()),
                    "patch_sha256": sha(patch),
                    "test": selection,
                    "required_assertion": marker,
                    "status": status,
                    **result,
                }
            )
        finally:
            target.write_bytes(original)
            for cached in target.parent.glob("__pycache__/" + target.stem + ".*.pyc"):
                cached.unlink()
            persist()
    report["all_named_experiments_detected"] = len(report["experiments"]) == len(CASES) and all(
        item["status"] == "detected-by-behavioral-assertion" for item in report["experiments"]
    )
    report["source_restored"] = all(
        (export / filename).read_bytes()
        == subprocess.check_output(["git", "show", source + ":" + filename], cwd=root)
        for filename in {case[1] for case in CASES}
    )
    persist()
    print(
        json.dumps(
            {
                "experiments": len(CASES),
                "all_named_experiments_detected": report["all_named_experiments_detected"],
                "source_restored": report["source_restored"],
                "mutmut_executed": False,
            }
        )
    )
    raise SystemExit(
        0 if report["all_named_experiments_detected"] and report["source_restored"] else 1
    )


if __name__ == "__main__":
    main()
