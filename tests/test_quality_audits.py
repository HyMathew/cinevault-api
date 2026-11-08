import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import audit_dependencies


def test_audit_child_output_is_utf8_even_with_legacy_parent_encoding(monkeypatch, tmp_path):
    import sys

    monkeypatch.setattr(
        audit_dependencies, "__file__", str(tmp_path / "scripts/audit_dependencies.py")
    )
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    original_run = subprocess.run

    def diagnostic_child(command, **kwargs):
        return original_run(
            [sys.executable, "-c", "import sys; sys.stderr.write(chr(232)); print('{}')"],
            **kwargs,
        )

    monkeypatch.setattr(audit_dependencies.subprocess, "run", diagnostic_child)
    with pytest.raises(SystemExit) as result:
        audit_dependencies.main()
    assert result.value.code == 0
    summary = json.loads((tmp_path / "reports/dependency-summary.json").read_text())
    assert all(item["exit_code"] == 0 for item in summary)
    assert (tmp_path / "reports/requirements-quality-audit.log").read_text(encoding="utf-8") == "è"


def test_timeout_invalidates_previous_success_and_records_each_attempt(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "dependency-summary.json").write_text('[{"exit_code":0}]')
    monkeypatch.setattr(
        audit_dependencies, "__file__", str(tmp_path / "scripts/audit_dependencies.py")
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr(audit_dependencies.subprocess, "run", timeout)
    with pytest.raises(SystemExit) as exc:
        audit_dependencies.main()
    assert exc.value.code == 1
    summary = json.loads((reports / "dependency-summary.json").read_text())
    assert len(summary) == 3
    assert all(item["exit_code"] != 0 and item.get("error") for item in summary)


def test_lock_must_list_active_transitive_dependencies_even_if_installed(monkeypatch, tmp_path):
    from scripts import check_lock

    requirements = tmp_path / "requirements.txt"
    requirements.write_text("parent-package==1.0\n")
    graph = {
        "parent-package": SimpleNamespace(
            version="1.0", requires=['child-package>=2; sys_platform == "win32"']
        ),
        "child-package": SimpleNamespace(version="2.0", requires=[]),
    }
    monkeypatch.setattr(check_lock.metadata, "distribution", lambda name: graph[name])
    assert check_lock.lock_errors(requirements, environment={"sys_platform": "linux"}) == []
    errors = check_lock.lock_errors(requirements, environment={"sys_platform": "win32"})
    assert errors and "child-package" in " ".join(errors)
    requirements.write_text("parent-package==1.0\nchild-package==2.0\n")
    assert check_lock.lock_errors(requirements, environment={"sys_platform": "win32"}) == []
