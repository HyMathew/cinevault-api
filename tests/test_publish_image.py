"""Publishing must reuse a gated identity and leave no registry credential behind."""

import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    gate = {
        "image": "cinevault-api:ci-" + REVISION,
        "image_id": IMAGE_ID,
        "health_passed": True,
        "scan_passed": True,
        "cleanup_passed": True,
        "scanner_version": "0.121.2",
        "scanner_exit_code": 0,
        "severity_counts": {"Medium": 5, "Low": 1},
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
