"""Build, run non-root health checks, then scan an immutable image archive."""

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

SCANNER = (
    "anchore/grype:v0.121.2@sha256:6a36234da13f9faa327306ac0b3dab3405cfa2b4eb20c8f904125a8707863913"
)
CACHE_HELPER = (
    "python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc"
)


def cleanup_volume(name, attempt):
    item = {"volume": name, "success": False}
    try:
        inspected = subprocess.run(
            ["docker", "volume", "inspect", name], text=True, capture_output=True, timeout=30
        )
        if inspected.returncode:
            item["success"] = "no such volume" in inspected.stderr.lower()
            item["exit_code"] = inspected.returncode
            return item
        values = json.loads(inspected.stdout)
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            item["error"] = "Malformed volume inspection; removal refused"
            return item
        info = values[0]
        labels = info.get("Labels") or {}
        if (
            info["Name"] != name
            or labels.get("cinevault.qa") != "image-audit"
            or labels.get("cinevault.attempt") != attempt
        ):
            item["error"] = "Ownership mismatch; volume removal refused"
            return item
        result = subprocess.run(
            ["docker", "volume", "rm", name], text=True, capture_output=True, timeout=30
        )
        item["exit_code"] = result.returncode
        item["success"] = result.returncode == 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        item["error"] = type(exc).__name__
    return item


def command(args, *, log=None, env=None, timeout=600):
    result = subprocess.run(
        args,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    if log:
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(
            f"{args[0]} {args[1]} failed with exit {result.returncode}; inspect reports"
        )
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Local application image tag to build")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/:@-]+", args.image):
        parser.error("Invalid image reference")
    root = Path(__file__).resolve().parents[1]
    reports_root = root / "reports"
    reports_root.mkdir(exist_ok=True)
    attempt = uuid.uuid4().hex
    reports = reports_root / ("image-" + attempt)
    reports.mkdir()
    receipt = {
        "image": args.image,
        "scanner": SCANNER,
        "scan_passed": False,
        "health_passed": False,
        "attempt_directory": reports.name,
        "started_at": datetime.now(UTC).isoformat(),
    }
    container = "cinevault-ci-" + attempt
    scanner_container = container + "-scanner"
    helper_container = container + "-init"
    cache_volume = container + "-cache"
    volume_attempted = False

    def save_receipt():
        value = json.dumps(receipt, indent=2)
        (reports / "image-check.json").write_text(value, encoding="utf-8")
        (reports_root / "image-check.json").write_text(value, encoding="utf-8")

    save_receipt()
    created = False
    try:
        iidfile = reports / "build.iid"
        command(
            [
                "docker",
                "build",
                "--pull",
                "--iidfile",
                str(iidfile),
                "--tag",
                args.image,
                str(root),
            ],
            log=reports / "docker-build.log",
        )
        identity = iidfile.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
            raise RuntimeError("Build did not produce a valid immutable image identity")
        info = json.loads(command(["docker", "image", "inspect", identity]))[0]
        if info["Id"] != identity:
            raise RuntimeError("Inspected image differs from the build receipt")
        receipt["image_id"] = info["Id"]
        receipt["configured_user"] = info["Config"]["User"]
        if not receipt["configured_user"] or receipt["configured_user"].split(":")[0] in (
            "root",
            "0",
        ):
            raise RuntimeError("Application image must use a non-root user")
        environment = {**os.environ, "SECRET_KEY": secrets.token_urlsafe(48)}
        created = True
        command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=67108864",
                "--health-interval",
                "1s",
                "--health-start-period",
                "1s",
                "--health-retries",
                "5",
                "-e",
                "SECRET_KEY",
                "-e",
                "ENTERPRISE_ENABLED=false",
                "-e",
                "DATA_FILE=/tmp/cinevault-smoke.json",
                receipt["image_id"],
            ],
            env=environment,
        )
        receipt["runtime_uid"] = int(
            command(
                ["docker", "exec", container, "python", "-c", "import os; print(os.getuid())"]
            ).strip()
        )
        if receipt["runtime_uid"] == 0:
            raise RuntimeError("Application process must not run as root")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            health = json.loads(
                command(["docker", "inspect", "--format", "{{json .State.Health}}", container])
            )
            (reports / "container-health.json").write_text(
                json.dumps(health, indent=2), encoding="utf-8"
            )
            if health and health["Status"] == "healthy":
                receipt["health_passed"] = True
                break
            if health and health["Status"] == "unhealthy":
                break
            time.sleep(1)
        if not receipt["health_passed"]:
            raise RuntimeError("Container health check did not become healthy")
        command(["docker", "rm", "--force", container])
        created = False
        volume_attempted = True
        receipt["cache_volume"] = cache_volume
        command(
            [
                "docker",
                "volume",
                "create",
                "--label",
                "cinevault.qa=image-audit",
                "--label",
                "cinevault.attempt=" + attempt,
                cache_volume,
            ]
        )
        command(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                helper_container,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,source={cache_volume},target=/cache",
                "--entrypoint",
                "python",
                CACHE_HELPER,
                "-c",
                "import os; os.mkdir('/cache/tmp',0o700); os.chmod('/cache',0o700); os.chown('/cache/tmp',65532,65532); os.chown('/cache',65532,65532)",
            ],
            log=reports / "cache-init.log",
        )
        # The scanner receives only this archive. No daemon socket or host credentials.
        with tempfile.TemporaryDirectory(prefix="cinevault-image-") as directory:
            archive = Path(directory) / "image.tar"
            command(["docker", "image", "save", "--output", str(archive), receipt["image_id"]])
            scan = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    scanner_container,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--read-only",
                    "--user",
                    "65532:65532",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=67108864,mode=1777",
                    "-e",
                    "TMPDIR=/cache/tmp",
                    "-e",
                    "GRYPE_DB_CACHE_DIR=/cache/grype",
                    "-e",
                    "GRYPE_CHECK_FOR_APP_UPDATE=false",
                    "--mount",
                    f"type=bind,source={archive},target=/image.tar,readonly",
                    "--mount",
                    f"type=volume,source={cache_volume},target=/cache",
                    SCANNER,
                    "docker-archive:/image.tar",
                    "--output",
                    "json",
                    "--fail-on",
                    "high",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
            )
            (reports / "image-vulnerabilities.json").write_text(scan.stdout, encoding="utf-8")
            (reports / "image-scan.log").write_text(scan.stderr, encoding="utf-8")
            receipt["scanner_exit_code"] = scan.returncode
            try:
                report = json.loads(scan.stdout)
                receipt["severity_counts"] = dict(
                    Counter(m["vulnerability"]["severity"] for m in report["matches"])
                )
                receipt["database"] = report["descriptor"]["db"]
                receipt["scanner_version"] = report["descriptor"]["version"]
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("Scanner report missing or malformed; scan cannot pass") from exc
            if receipt["scanner_version"] != "0.121.2" or scan.returncode:
                raise RuntimeError("Image scan blocked; inspect the complete vulnerability report")
            receipt["scan_passed"] = True
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        receipt["error"] = str(exc)
        print(str(exc))
        raise SystemExit(1) from exc
    finally:
        if created:
            try:
                logs = subprocess.run(
                    ["docker", "logs", container], text=True, capture_output=True, timeout=30
                )
                (reports / "smoke-container.log").write_text(
                    logs.stdout + logs.stderr, encoding="utf-8"
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                receipt["log_error"] = type(exc).__name__
        receipt["cleanup"] = []
        for name in (scanner_container, helper_container, container):
            item = {"container": name, "success": False}
            try:
                result = subprocess.run(
                    ["docker", "rm", "--force", name], text=True, capture_output=True, timeout=30
                )
                item["exit_code"] = result.returncode
                item["success"] = result.returncode == 0 or any(
                    message in result.stderr for message in ("No such container", "No such object")
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                item["error"] = type(exc).__name__
            receipt["cleanup"].append(item)
        receipt["cleanup_passed"] = all(item["success"] for item in receipt["cleanup"])
        if volume_attempted:
            receipt["volume_cleanup"] = cleanup_volume(cache_volume, attempt)
            receipt["cleanup_passed"] = (
                receipt["cleanup_passed"] and receipt["volume_cleanup"]["success"]
            )
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        save_receipt()
        if not receipt["cleanup_passed"]:
            print("Container cleanup could not be confirmed; inspect the current receipt.")
            raise SystemExit(1)
    print("Image build, non-root health, vulnerability gate and cleanup passed.")


if __name__ == "__main__":
    main()
