import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_image


@pytest.mark.parametrize("cleanup_timeout", [False, True])
def test_failed_run_always_cleans_both_names_and_writes_current_receipt(
    monkeypatch, tmp_path, cleanup_timeout
):
    monkeypatch.setattr(check_image, "__file__", str(tmp_path / "scripts/check_image.py"))
    monkeypatch.setattr(sys, "argv", ["check_image", "--image", "example/qa:local"])
    monkeypatch.setattr(check_image.os, "environ", {})
    calls = []
    run_attempted = []

    def command(args, **kwargs):
        if args[1] == "build":
            Path(args[args.index("--iidfile") + 1]).write_text("sha256:" + "a" * 64)
        if args[1:3] == ["image", "inspect"]:
            return json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {"User": "10001"}}])
        if args[1] == "run":
            run_attempted.append(args[args.index("--name") + 1])
            raise subprocess.TimeoutExpired(args, 1)
        return ""

    def cleanup(args, **kwargs):
        calls.append(args)
        if args[1] == "rm" and args[-1].endswith("-scanner") and cleanup_timeout:
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(check_image, "command", command)
    monkeypatch.setattr(check_image.subprocess, "run", cleanup)
    with pytest.raises(SystemExit) as exc:
        check_image.main()
    assert exc.value.code == 1
    assert len(run_attempted) == 1
    removed = [args[-1] for args in calls if args[1] == "rm"]
    assert any(name.endswith("-scanner") for name in removed)
    assert any(not name.endswith("-scanner") for name in removed)
    assert run_attempted[0] in removed
    receipt = json.loads((tmp_path / "reports/image-check.json").read_text())
    assert receipt["scan_passed"] is False and receipt["error"]


def test_image_identity_comes_from_build_iidfile_not_mutable_tag(monkeypatch, tmp_path):
    monkeypatch.setattr(check_image, "__file__", str(tmp_path / "scripts/check_image.py"))
    monkeypatch.setattr(sys, "argv", ["check_image", "--image", "example/qa:local"])
    inspected = []

    def command(args, **kwargs):
        if args[1] == "build":
            assert "--iidfile" in args
            Path(args[args.index("--iidfile") + 1]).write_text("sha256:" + "a" * 64)
        elif args[1:3] == ["image", "inspect"]:
            inspected.append(args[-1])
            return json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {"User": "0"}}])
        return ""

    monkeypatch.setattr(check_image, "command", command)
    monkeypatch.setattr(
        check_image.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    with pytest.raises(SystemExit):
        check_image.main()
    assert inspected == ["sha256:" + "a" * 64]


@pytest.mark.parametrize("owned", [False, True, None])
def test_volume_cleanup_refuses_foreign_ownership(monkeypatch, owned):
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        if args[2] == "inspect":
            if owned is None:
                return subprocess.CompletedProcess(args, 0, "[]", "")
            labels = {
                "cinevault.qa": "image-audit",
                "cinevault.attempt": "mine" if owned else "someone-else",
            }
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"Name": "named-volume", "Labels": labels}]), ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(check_image.subprocess, "run", docker)
    result = check_image.cleanup_volume("named-volume", "mine")
    assert result["success"] is (owned is True)
    assert any(args[2] == "rm" for args in calls) is (owned is True)
