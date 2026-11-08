"""Publish the exact gated CI image; keep registry credentials ephemeral."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def context():
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    revision = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/cinevault-(?:api|web)", repository
    ) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid project or source revision")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    metadata = event.get("repository", {})
    branch = metadata.get("default_branch")
    if (
        metadata.get("full_name") != repository
        or not isinstance(branch, str)
        or not branch
        or os.environ.get("GITHUB_EVENT_NAME") not in ("push", "workflow_dispatch")
        or os.environ.get("GITHUB_REF") != "refs/heads/" + branch
    ):
        raise ValueError("Publication requires a trusted default-branch event")
    return repository, revision


def gated_image(root, repository, revision):
    gate = json.loads((root / "reports/image-check.json").read_text(encoding="utf-8"))
    image = repository.split("/")[1] + ":ci-" + revision
    identity = gate.get("image_id", "")
    counts = gate.get("severity_counts")
    if (
        gate.get("image") != image
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", identity)
        or any(
            gate.get(name) is not True
            for name in ("health_passed", "scan_passed", "cleanup_passed")
        )
        or gate.get("scanner_version") != "0.121.2"
        or type(gate.get("scanner_exit_code")) is not int
        or gate["scanner_exit_code"] != 0
        or not isinstance(counts, dict)
        or any(
            key not in ("Unknown", "Negligible", "Low", "Medium", "High", "Critical")
            or type(value) is not int
            or value < 0
            for key, value in counts.items()
        )
        or counts.get("High", 0)
        or counts.get("Critical", 0)
    ):
        raise ValueError("Image health, vulnerability or cleanup gate is not valid")
    return image, identity


def command(arguments, environment, *, input=None):
    result = subprocess.run(
        ["docker", *arguments],
        input=input,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=environment,
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError("Registry operation failed: " + arguments[0])
    return result.stdout + result.stderr


def publish(root, actor, token):
    root = Path(root)
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    destination = reports / "registry-publication.json"
    receipt = {"published": False, "scope": "Registry push; anonymous pull not yet verified"}
    destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    try:
        repository, revision = context()
        image, identity = gated_image(root, repository, revision)
        if (
            not isinstance(actor, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\[\]-]{0,99}", actor)
            or not isinstance(token, str)
            or not token
            or "\n" in token
            or "\r" in token
        ):
            raise ValueError("Registry identity is missing or malformed")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("REGISTRY_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
        }
        observed = command(["image", "inspect", "--format", "{{.Id}}", image], environment).strip()
        if observed != identity:
            raise RuntimeError("The local image no longer matches its gate receipt")
        tag = "ghcr.io/" + repository.lower() + ":sha-" + revision
        receipt.update(repository=repository, source_commit=revision, image_id=identity, tag=tag)
        with tempfile.TemporaryDirectory(
            prefix="cinevault-registry-", dir=os.environ.get("RUNNER_TEMP")
        ) as directory:
            environment["DOCKER_CONFIG"] = directory
            command(
                ["login", "ghcr.io", "--username", actor, "--password-stdin"],
                environment,
                input=token + "\n",
            )
            command(["tag", identity, tag], environment)
            output = command(["push", tag], environment)
            digests = set(re.findall(r"digest:\s+(sha256:[a-f0-9]{64})(?:\s|$)", output))
            if len(digests) != 1:
                raise RuntimeError("Registry did not acknowledge one manifest digest")
            digest = digests.pop()
            receipt.update(
                digest=digest,
                registry_ref="ghcr.io/" + repository.lower() + "@" + digest,
            )
        receipt["published"] = True
        return receipt
    except (
        KeyError,
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        receipt["error"] = type(exc).__name__
        raise
    finally:
        destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main():
    try:
        result = publish(
            Path(__file__).resolve().parents[1],
            os.environ.get("GITHUB_ACTOR"),
            os.environ.get("REGISTRY_TOKEN"),
        )
    except (
        KeyError,
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print("Image publication failed: " + type(exc).__name__ + "; inspect the receipt.")
        raise SystemExit(1) from exc
    print("Published " + result["registry_ref"] + "; anonymous access remains to be checked.")


if __name__ == "__main__":
    main()
