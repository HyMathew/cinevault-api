"""Publishing must reuse a gated identity and leave no registry credential behind."""

import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.image_archive import SCANNER, inspect_sbom
from tests.test_image_archive import digest, encoded, sbom_report, scan_report

REVISION = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64
DIGEST = "sha256:" + "c" * 64
TOKEN = "synthetic-registry-token-for-tests"


def publisher():
    assert importlib.util.find_spec("scripts.publish_image") is not None, (
        "The gated image publication helper has not been implemented"
    )
    return importlib.import_module("scripts.publish_image")


def setup_context(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"repository": {"full_name": "Example/cinevault-api", "default_branch": "main"}}
        ),
        encoding="utf-8",
    )
    for name, value in {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "Example/cinevault-api",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": REVISION,
        "RUNNER_TEMP": str(tmp_path),
        "REGISTRY_TOKEN": TOKEN,
    }.items():
        monkeypatch.setenv(name, value)
    reports = tmp_path / "reports"
    reports.mkdir()
    attempt = reports / ("image-" + "f" * 32)
    attempt.mkdir()
    config = b'{"architecture":"amd64","os":"linux"}'
    report = scan_report(config)
    report["matches"] = [{"vulnerability": {"severity": "Medium"}}] * 5 + [
        {"vulnerability": {"severity": "Low"}}
    ]
    raw = encoded(report)
    (attempt / "image-vulnerabilities.json").write_bytes(raw)
    inventory = encoded(sbom_report(config))
    (attempt / "image-sbom.json").write_bytes(inventory)
    gate = {
        "image": "cinevault-api:ci-" + REVISION,
        "image_id": IMAGE_ID,
        "attempt_directory": attempt.name,
        "health_passed": True,
        "scan_passed": True,
        "cleanup_passed": True,
        "scanner": SCANNER,
        "scanner_version": "0.121.2",
        "scanner_exit_code": 0,
        "severity_counts": {"Medium": 5, "Low": 1},
        "database": report["descriptor"]["db"],
        "scan_provenance": {
            "docker_image_id": IMAGE_ID,
            "archive_identity_kind": "oci-index",
            "archive_sha256": digest(b"scanned synthetic tar"),
            "archive_config_digest": digest(config),
            "archive_oci_manifest_digest": digest(b"original OCI manifest"),
            "scan_report_sha256": digest(raw),
            "grype_config_digest": digest(config),
            "grype_manifest_digest": report["source"]["target"]["manifestDigest"],
            **inspect_sbom(inventory, digest(config)),
        },
    }
    (reports / "image-check.json").write_text(json.dumps(gate), encoding="utf-8")
    return gate


def fake_docker(monkeypatch, module, calls, *, observed=IMAGE_ID, fail_push=False):
    def run(command, **kwargs):
        assert TOKEN not in " ".join(command)
        assert "REGISTRY_TOKEN" not in kwargs["env"]
        calls.append((command, kwargs))
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=observed + "\n", stderr="")
        if command[1] == "login":
            assert kwargs["input"] == TOKEN + "\n"
            path = Path(kwargs["env"]["DOCKER_CONFIG"])
            (path / "config.json").write_text("synthetic private auth data", encoding="utf-8")
        if command[1] == "push":
            return SimpleNamespace(
                returncode=int(fail_push),
                stdout="tag: digest: " + DIGEST + " size: 1234\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)


@pytest.mark.parametrize(
    "changes",
    [
        {"scan_passed": False},
        {"health_passed": False},
        {"cleanup_passed": False},
        {"severity_counts": {"High": 1}},
        {"severity_counts": {"high": 1}},
        {"scanner_exit_code": 1},
        {"image": "cinevault-api:ci-" + "d" * 40},
        {"image_id": "mutable-tag"},
        {"scanner_version": "unknown"},
        {"scan_provenance": None},
        {"attempt_directory": "../outside"},
    ],
)
def test_invalid_gate_refuses_before_registry_credentials(tmp_path, monkeypatch, changes):
    module = publisher()
    gate = setup_context(tmp_path, monkeypatch)
    (tmp_path / "reports/image-check.json").write_text(
        json.dumps({**gate, **changes}), encoding="utf-8"
    )
    calls = []
    fake_docker(monkeypatch, module, calls)
    with pytest.raises((ValueError, RuntimeError)):
        module.publish(tmp_path, "Example", TOKEN)
    assert not calls
    result = json.loads((tmp_path / "reports/registry-publication.json").read_text())
    assert result["published"] is False


@pytest.mark.parametrize(
    ("variable", "value"),
    [("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_REF", "refs/heads/feature")],
)
def test_untrusted_event_or_branch_refuses_before_login(tmp_path, monkeypatch, variable, value):
    module = publisher()
    setup_context(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, value)
    calls = []
    fake_docker(monkeypatch, module, calls)
    with pytest.raises((ValueError, RuntimeError)):
        module.publish(tmp_path, "Example", TOKEN)
    assert not calls


def test_changed_local_tag_is_rejected_before_login(tmp_path, monkeypatch):
    module = publisher()
    setup_context(tmp_path, monkeypatch)
    calls = []
    fake_docker(monkeypatch, module, calls, observed="sha256:" + "d" * 64)
    with pytest.raises((ValueError, RuntimeError)):
        module.publish(tmp_path, "Example", TOKEN)
    assert len(calls) == 1 and calls[0][0][1:3] == ["image", "inspect"]


@pytest.mark.parametrize(
    ("owner", "event", "ref", "expected", "permitted"),
    [
        ("HyMathew", "workflow_dispatch", "refs/heads/quality/cinevault-v2", REVISION, True),
        ("HyMathew", "push", "refs/heads/quality/cinevault-v2", REVISION, False),
        ("HyMathew", "workflow_dispatch", "refs/heads/quality/cinevault-v2", "", False),
        ("HyMathew", "workflow_dispatch", "refs/heads/quality/cinevault-v2", "d" * 40, False),
        ("HyMathew", "workflow_dispatch", "refs/heads/feature", REVISION, False),
        ("Fork", "workflow_dispatch", "refs/heads/quality/cinevault-v2", REVISION, False),
    ],
)
def test_candidate_dispatch_is_bound_to_the_official_branch_and_full_commit(
    tmp_path, monkeypatch, owner, event, ref, expected, permitted
):
    module = publisher()
    setup_context(tmp_path, monkeypatch)
    repo = owner + "/cinevault-api"
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "repository": {"full_name": repo, "default_branch": "main"},
                "inputs": {"candidate_commit": expected},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    calls = []
    fake_docker(monkeypatch, module, calls)
    if permitted:
        receipt = module.publish(tmp_path, "Example", TOKEN)
        assert receipt["published"] and receipt["publication_channel"] == "candidate"
        assert receipt["tag"] == "ghcr.io/hymathew/cinevault-api:sha-" + REVISION
    else:
        with pytest.raises(ValueError):
            module.publish(tmp_path, "Example", TOKEN)
        assert not calls


@pytest.mark.parametrize(
    "defect",
    ["report_bytes", "report_missing", "report_severity", "config", "identity", "archive_digest"],
)
def test_scan_provenance_mismatch_refuses_before_login(tmp_path, monkeypatch, defect):
    module = publisher()
    gate = setup_context(tmp_path, monkeypatch)
    reports = tmp_path / "reports"
    report_path = reports / gate["attempt_directory"] / "image-vulnerabilities.json"
    provenance = gate["scan_provenance"]
    if defect == "report_bytes":
        report_path.write_bytes(report_path.read_bytes() + b"\n")
    if defect == "report_missing":
        report_path.unlink()
    if defect == "report_severity":
        report = json.loads(report_path.read_bytes())
        report["matches"][0]["vulnerability"]["severity"] = "High"
        raw = encoded(report)
        report_path.write_bytes(raw)
        provenance["scan_report_sha256"] = digest(raw)
    if defect == "config":
        provenance["archive_config_digest"] = digest(b"wrong config")
        provenance["grype_config_digest"] = provenance["archive_config_digest"]
    if defect == "identity":
        provenance["docker_image_id"] = digest(b"wrong Docker image")
    if defect == "archive_digest":
        provenance["archive_sha256"] = "not-a-digest"
    (reports / "image-check.json").write_text(json.dumps(gate), encoding="utf-8")
    calls = []
    fake_docker(monkeypatch, module, calls)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        module.publish(tmp_path, "Example", TOKEN)
    assert not calls


def test_push_uses_exact_gated_id_and_destroys_private_docker_config(tmp_path, monkeypatch):
    module = publisher()
    setup_context(tmp_path, monkeypatch)
    calls = []
    fake_docker(monkeypatch, module, calls)
    result = module.publish(tmp_path, "Example", TOKEN)
    tag = "ghcr.io/example/cinevault-api:sha-" + REVISION
    assert next(args for args, _ in calls if args[1] == "tag") == ["docker", "tag", IMAGE_ID, tag]
    assert result["published"] is True
    assert result["registry_ref"] == "ghcr.io/example/cinevault-api@" + DIGEST
    assert result["source_commit"] == REVISION
    assert result["scan_provenance"]["docker_image_id"] == IMAGE_ID
    configuration = next(kw["env"]["DOCKER_CONFIG"] for args, kw in calls if args[1] == "login")
    assert not Path(configuration).exists()


def test_new_attempt_invalidates_old_receipt_before_any_docker_work(tmp_path, monkeypatch):
    module = publisher()
    setup_context(tmp_path, monkeypatch)
    receipt = tmp_path / "reports/registry-publication.json"
    receipt.write_text('{"published":true,"digest":"old"}', encoding="utf-8")
    calls = []
    fake_docker(monkeypatch, module, calls)
    actual = module.subprocess.run

    def inspect_receipt(command, **kwargs):
        assert json.loads(receipt.read_text())["published"] is False
        return actual(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", inspect_receipt)
    module.publish(tmp_path, "Example", TOKEN)
    assert TOKEN not in (tmp_path / "reports/registry-publication.json").read_text()


def test_registry_digest_survives_cleanup_error_after_acknowledged_push(tmp_path, monkeypatch):
    from contextlib import contextmanager

    module = publisher()
    setup_context(tmp_path, monkeypatch)
    calls = []
    fake_docker(monkeypatch, module, calls)
    original = module.tempfile.TemporaryDirectory

    @contextmanager
    def cleanup_error(*args, **kwargs):
        with original(*args, **kwargs) as directory:
            yield directory
        raise OSError("Synthetic cleanup error after registry acknowledgement")

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", cleanup_error)
    with pytest.raises(OSError):
        module.publish(tmp_path, "Example", TOKEN)
    result = json.loads((tmp_path / "reports/registry-publication.json").read_text())
    assert result["published"] is False
    assert result["digest"] == DIGEST
    assert result["registry_ref"].endswith("@" + DIGEST)


def test_failed_push_invalidates_success_and_destroys_credentials(tmp_path, monkeypatch):
    module = publisher()
    setup_context(tmp_path, monkeypatch)
    receipt = tmp_path / "reports/registry-publication.json"
    receipt.write_text('{"published":true,"digest":"old"}', encoding="utf-8")
    calls = []
    fake_docker(monkeypatch, module, calls, fail_push=True)
    with pytest.raises((ValueError, RuntimeError)):
        module.publish(tmp_path, "Example", TOKEN)
    result = json.loads(receipt.read_text())
    assert result["published"] is False
    configuration = next(kw["env"]["DOCKER_CONFIG"] for args, kw in calls if args[1] == "login")
    assert not Path(configuration).exists()


def test_tampered_sbom_refuses_before_registry_credentials(tmp_path, monkeypatch):
    module = publisher()
    gate = setup_context(tmp_path, monkeypatch)
    dest = tmp_path / "reports" / gate["attempt_directory"] / "image-sbom.json"
    dest.write_bytes(dest.read_bytes() + b"\n")
    calls = []
    fake_docker(monkeypatch, module, calls)
    with pytest.raises(ValueError, match="no longer matches"):
        module.publish(tmp_path, "Example", TOKEN)
    assert not calls
