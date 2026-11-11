"""Run the real MongoDB contract on an owned, authenticated disposable replica set."""

import base64
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

IMAGE = "mongo@sha256:1d7a2d487c6465ea022474b90b65d6c91bb600b421b65880d8c6e141d6122200"
LABEL = "cinevault.mongodb-contract"
REPLICA_SET = "cinevault-ci"


def verify_kernel(release):
    """Refuse the upstream tcmalloc-incompatible range; never bypass mongod's check."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", release)
    if match is None:
        raise RuntimeError("Unrecognized Linux kernel release")
    version = tuple(map(int, match.groups()))
    if (6, 19, 0) <= version < (7, 0, 14):
        raise RuntimeError("Host kernel is in MongoDB's unsupported 6.19 through 7.0.13 range")


class Diagnostics:
    """Bounded, sanitized diagnostics only; never retain child argv or stdin."""

    def __init__(self, receipt, path, sensitive):
        self.receipt, self.path, self.sensitive = receipt, path, sensitive

    def save(self):
        self.path.write_text(json.dumps(self.receipt, indent=2), encoding="utf-8")

    def stage(self, name):
        self.receipt["stage"] = name
        self.save()

    def failure(self, *, stdout="", stderr="", returncode=None, timeout=False):
        def safe(value):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return redact(value or "", self.sensitive)[-4096:]

        self.receipt["last_child_failure"] = {
            "stage": self.receipt["stage"],
            "returncode": returncode,
            "timeout": timeout,
            "stdout_tail": safe(stdout),
            "stderr_tail": safe(stderr),
        }
        self.save()


def command(arguments, *, data=None, env=None, timeout=120, diagnostics=None):
    """Capture all child output: failures must never interpolate secret input/URIs."""
    try:
        result = subprocess.run(
            arguments,
            input=data,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        if diagnostics:
            diagnostics.failure(stdout=exc.stdout, stderr=exc.stderr, timeout=True)
        raise RuntimeError("Child command timed out (output sanitized in receipt)") from None
    if result.returncode:
        if diagnostics:
            diagnostics.failure(
                stdout=result.stdout, stderr=result.stderr, returncode=result.returncode
            )
        raise RuntimeError("Child command failed (output sanitized in receipt)")
    return result.stdout


def redact(text, values):
    for value in sorted(values, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    # Also remove an unexpected URI, including credentials not in the known list.
    return re.sub(r"mongodb(?:\+srv)?://[^\s\"'<>]+", "[MONGODB_URI]", text)


def verify_junit(path):
    """A missing, empty, skipped or failing real-engine campaign is a failed gate."""
    cases = list(ET.parse(path).getroot().iter("testcase"))
    if len(cases) < 5 or any(
        case.find(name) is not None for case in cases for name in ("skipped", "failure", "error")
    ):
        raise RuntimeError("Real MongoDB tests missing, skipped or failed")
    return len(cases)


def cleanup_container(name, attempt):
    inspected = subprocess.run(
        ["docker", "container", "inspect", name], capture_output=True, text=True, timeout=30
    )
    if inspected.returncode:
        return "no such container" in inspected.stderr.lower()
    values = json.loads(inspected.stdout)
    if len(values) != 1:
        return False
    item = values[0]
    if (
        item.get("Name") != "/" + name
        or item.get("Config", {}).get("Labels", {}).get(LABEL) != attempt
    ):
        return False
    result = subprocess.run(
        ["docker", "container", "rm", "--force", name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0


def wait_shell(container, script, *, diagnostics=None):
    for attempt in range(60):
        try:
            shell(container, script, diagnostics=diagnostics)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            if diagnostics:
                diagnostics.receipt["wait_attempts"] = attempt + 1
                diagnostics.save()
            time.sleep(1)
    raise RuntimeError("Disposable MongoDB startup deadline exceeded")


def shell(container, script, *, diagnostics=None):
    return command(
        [
            "docker",
            "exec",
            "-i",
            "--env",
            "HOME=/tmp",
            container,
            "mongosh",
            "--quiet",
            "--norc",
            "--file",
            "/dev/stdin",
        ],
        data=script,
        timeout=10,
        diagnostics=diagnostics,
    )


def capture_server_diagnostics(container, attempt, diagnostics):
    """Inspect/log only the exact owned server, before cleanup removes it."""
    raw = command(["docker", "container", "inspect", container], timeout=10)
    info = json.loads(raw)[0]
    if (
        info.get("Name") != "/" + container
        or info.get("Config", {}).get("Labels", {}).get(LABEL) != attempt
    ):
        diagnostics.receipt["server_diagnostics"] = "ownership mismatch; refused"
        return
    state = info.get("State", {})
    diagnostics.receipt["server_state"] = {
        name: state.get(name) for name in ("Status", "Running", "OOMKilled", "ExitCode")
    }
    diagnostics.receipt["server_state"]["Error"] = redact(
        str(state.get("Error", "")), diagnostics.sensitive
    )[-4096:]
    logs = subprocess.run(
        ["docker", "logs", "--tail", "80", container],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
    )
    diagnostics.receipt["server_log_tail"] = redact(
        logs.stdout + logs.stderr, diagnostics.sensitive
    )[-12288:]
    diagnostics.receipt["server_log_exit_code"] = logs.returncode
    diagnostics.save()


def denied(action):
    from pymongo.errors import OperationFailure

    try:
        action()
    except OperationFailure as exc:
        if exc.code == 13:
            return
    raise AssertionError("Expected authorization refusal 13")


def application_contract(client, address, password, attempt):
    """Maintenance provisions; the application receives only collection CRUD."""
    from pymongo import MongoClient

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from app.persistence.mongo_model import collection_name
    from app.storage_v2 import COLLECTIONS

    spec = importlib.util.spec_from_file_location(
        "mongo_contract", root / "tests/test_mongo_persistence.py"
    )
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    name = "cinevault_app_contract_" + attempt
    database = client[name]
    collections = [collection_name(item) for item in COLLECTIONS] + ["coordination", "entity_ids"]
    database.command(
        "createRole",
        "application",
        privileges=[
            {
                "resource": {"db": name, "collection": item},
                "actions": ["find", "insert", "update", "remove"],
            }
            for item in collections
        ],
        roles=[],
    )
    database.command(
        "createUser", "application", pwd=password, roles=[{"role": "application", "db": name}]
    )
    options = {
        "replicaSet": REPLICA_SET,
        "directConnection": True,
        "serverSelectionTimeoutMS": 5000,
        "connectTimeoutMS": 5000,
        "socketTimeoutMS": 10000,
    }
    with MongoClient(
        address, username="application", password=password, authSource=name, **options
    ) as app:
        result = contract.exercise_backend(app[name], maintenance_database=database)
        for filename, function, key in (
            ("test_authentication_service.py", "exercise_auth_service", "authentication_service"),
            ("test_catalogue_service.py", "exercise_catalogue_service", "catalogue_service"),
            ("test_copy_service.py", "exercise_copy_service", "copy_service"),
        ):
            service_spec = importlib.util.spec_from_file_location(
                "cinevault_native_" + key, root / "tests" / filename
            )
            service_contract = importlib.util.module_from_spec(service_spec)
            service_spec.loader.exec_module(service_contract)
            result[key] = getattr(service_contract, function)(app[name])
        denied(lambda: app[name].users.create_index("display_name"))
        denied(lambda: app[name].command("createUser", "forbidden", pwd="unused", roles=[]))
        denied(lambda: app["other_database"].users.find_one({}))
    with MongoClient(address, **options) as anonymous:
        denied(lambda: anonymous[name].users.find_one({}))
    client.drop_database(name)
    return {**result, "ddl_denied": True, "cross_database_denied": True, "anonymous_denied": True}


def main():
    if sys.platform != "linux" or os.getuid() == 0:
        raise RuntimeError("This disposable Docker gate requires a non-root Linux host user")
    root = Path(__file__).resolve().parents[1]
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    attempt = uuid.uuid4().hex
    container = "cinevault-mongo-contract-" + attempt
    workspace = Path(tempfile.mkdtemp(prefix="cinevault-mongo-contract-"))
    workspace.chmod(0o700)
    secret = workspace / "keyfile"
    secret.write_text(base64.b64encode(secrets.token_bytes(756)).decode("ascii"), encoding="ascii")
    secret.chmod(0o400)
    data = workspace / "data"
    data.mkdir(mode=0o700)
    maintenance_password, app_password = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    sensitive = [maintenance_password, app_password, secret.read_text(encoding="ascii")]
    receipt = {"image": IMAGE, "passed": False, "started_at": datetime.now(UTC).isoformat()}
    diagnostics = Diagnostics(receipt, reports / "mongodb-check.json", sensitive)
    try:
        diagnostics.stage("host_kernel")
        receipt["host_kernel"] = os.uname().release
        verify_kernel(receipt["host_kernel"])
        diagnostics.stage("pull_image")
        command(["docker", "pull", IMAGE], timeout=600, diagnostics=diagnostics)
        diagnostics.stage("start_server")
        command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--label",
                LABEL + "=" + attempt,
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "768m",
                "--cpus",
                "0.75",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=67108864",
                "--tmpfs",
                "/data/configdb:rw,noexec,nosuid,size=16777216",
                "--mount",
                f"type=bind,src={secret},dst=/run/secrets/mongo-key,readonly",
                "--mount",
                f"type=bind,src={data},dst=/data/db",
                "--publish",
                "127.0.0.1::27017",
                "--entrypoint",
                "mongod",
                IMAGE,
                "--replSet",
                REPLICA_SET,
                "--bind_ip_all",
                "--auth",
                "--keyFile",
                "/run/secrets/mongo-key",
                "--wiredTigerCacheSizeGB",
                "0.256",
                "--oplogSize",
                "128",
            ],
            diagnostics=diagnostics,
        )
        diagnostics.stage("wait_ping")
        wait_shell(
            container, "if (!db.adminCommand({ping:1}).ok) quit(1);", diagnostics=diagnostics
        )
        diagnostics.stage("initiate_replica_set")
        shell(
            container,
            f'rs.initiate({{_id:"{REPLICA_SET}",members:[{{_id:0,host:"localhost:27017"}}]}});',
            diagnostics=diagnostics,
        )
        diagnostics.stage("wait_primary")
        wait_shell(
            container,
            "if (!db.adminCommand({hello:1}).isWritablePrimary) quit(1);",
            diagnostics=diagnostics,
        )
        diagnostics.stage("create_maintenance_identity")
        shell(
            container,
            'db.getSiblingDB("admin").createUser({user:"maintenance",pwd:'
            + json.dumps(maintenance_password)
            + ',roles:["root"]});',
            diagnostics=diagnostics,
        )
        diagnostics.stage("inspect_loopback_binding")
        info = json.loads(
            command(["docker", "container", "inspect", container], diagnostics=diagnostics)
        )[0]
        bindings = info["NetworkSettings"]["Ports"]["27017/tcp"]
        if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
            raise RuntimeError("MongoDB host port is not exclusively loopback")
        port = int(bindings[0]["HostPort"])
        address = f"mongodb://127.0.0.1:{port}"
        uri = (
            f"mongodb://maintenance:{maintenance_password}@127.0.0.1:{port}/"
            f"?authSource=admin&replicaSet={REPLICA_SET}&directConnection=true"
        )
        sensitive.append(uri)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            for value in sensitive:
                print("::add-mask::" + value, flush=True)
        from pymongo import MongoClient

        diagnostics.stage("verify_server_version")
        with MongoClient(uri, serverSelectionTimeoutMS=5000, socketTimeoutMS=10000) as client:
            build = client.admin.command("buildInfo")
            if build["version"] != "8.0.34":
                raise RuntimeError("Pinned MongoDB server version mismatch")
            receipt["server_version"] = build["version"]
            diagnostics.stage("application_privilege_contract")
            receipt["application_contract"] = application_contract(
                client, address, app_password, attempt
            )
        diagnostics.stage("real_pytest_contract")
        junit = reports / "mongodb-tests.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_mongo_persistence.py::RealMongoTests",
                "-W",
                "error",
                "--junitxml=" + str(junit),
            ],
            cwd=root,
            env={**os.environ, "MONGODB_TEST_URI": uri},
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=180,
        )
        (reports / "mongodb-tests.log").write_text(
            redact(result.stdout + result.stderr, sensitive), encoding="utf-8"
        )
        if junit.exists():
            junit.write_text(redact(junit.read_text(encoding="utf-8"), sensitive), encoding="utf-8")
        if result.returncode:
            raise RuntimeError("Real MongoDB pytest contract failed; inspect sanitized reports")
        receipt["real_tests_passed"] = verify_junit(junit)
        receipt["passed"] = True
        diagnostics.stage("contracts_passed")
    except Exception as exc:
        receipt["error_type"] = type(exc).__name__
        receipt["failed_stage"] = receipt.get("stage", "initialization")
        try:
            capture_server_diagnostics(container, attempt, diagnostics)
        except Exception as diagnostic_error:
            receipt["diagnostic_error_type"] = type(diagnostic_error).__name__
        raise RuntimeError("MongoDB gate failed; inspect the sanitized receipt") from None
    finally:
        try:
            receipt["container_removed"] = cleanup_container(container, attempt)
        except Exception as exc:
            receipt["container_removed"] = False
            receipt["cleanup_error_type"] = type(exc).__name__
        # Only this private mkdtemp tree is removed, after the owned server stopped.
        receipt["temporary_data_removed"] = False
        if receipt["container_removed"]:
            try:
                shutil.rmtree(workspace)
                receipt["temporary_data_removed"] = True
            except OSError as exc:
                receipt["cleanup_error_type"] = type(exc).__name__
        if not receipt["container_removed"] or not receipt["temporary_data_removed"]:
            receipt["passed"] = False
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        (reports / "mongodb-check.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        if not receipt["container_removed"] or not receipt["temporary_data_removed"]:
            raise RuntimeError("Owned MongoDB cleanup failed; gate refused")
    print("Authenticated real MongoDB contract passed; owned container and data removed")


if __name__ == "__main__":
    main()
