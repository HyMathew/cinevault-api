"""Safety boundaries of the real-engine gate; no mocked engine correctness claims."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.check_mongodb import (
    LABEL,
    Diagnostics,
    cleanup_container,
    command,
    redact,
    verify_junit,
)


class MongoGateBoundaryTests(unittest.TestCase):
    def test_kernel_incompatibility_is_rejected_without_bypassing_server_protection(self):
        from scripts import check_mongodb

        verify_kernel = getattr(check_mongodb, "verify_kernel", None)
        self.assertTrue(callable(verify_kernel), "The gate must check the documented kernel range")
        for release in ("6.19.0-generic", "6.20.0", "7.0.0-1017-azure", "7.0.13", "unknown"):
            with self.subTest(release=release), self.assertRaises(RuntimeError):
                verify_kernel(release)
        for release in ("6.8.0-1070-azure", "6.18.17", "7.0.14", "7.1.0"):
            with self.subTest(release=release):
                verify_kernel(release)

    def test_container_with_another_owner_is_never_removed(self):
        inspected = SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"Name": "/owned", "Config": {"Labels": {LABEL: "other"}}}]),
        )
        with patch("scripts.check_mongodb.subprocess.run", return_value=inspected) as run:
            self.assertFalse(cleanup_container("owned", "attempt"))
        self.assertEqual(run.call_count, 1)

    def test_exact_name_and_label_are_both_required_for_removal(self):
        inspected = SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"Name": "/owned", "Config": {"Labels": {LABEL: "attempt"}}}]),
        )
        with patch(
            "scripts.check_mongodb.subprocess.run",
            side_effect=[inspected, SimpleNamespace(returncode=0)],
        ) as run:
            self.assertTrue(cleanup_container("owned", "attempt"))
        self.assertEqual(run.call_args.args[0], ["docker", "container", "rm", "--force", "owned"])

    def test_missing_container_is_safe_but_daemon_failure_is_not_cleanup(self):
        for error, expected in (("No such container: owned", True), ("daemon unreachable", False)):
            with (
                self.subTest(error=error),
                patch(
                    "scripts.check_mongodb.subprocess.run",
                    return_value=SimpleNamespace(returncode=1, stderr=error),
                ),
            ):
                self.assertEqual(cleanup_container("owned", "attempt"), expected)

    def test_skipped_incomplete_and_failing_native_campaigns_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tests.xml"
            for suffix in ("<skipped/>", "<failure/>", "<error/>"):
                path.write_text(
                    "<testsuite>"
                    + '<testcase name="ok"/>' * 4
                    + "<testcase>"
                    + suffix
                    + "</testcase></testsuite>",
                    encoding="utf-8",
                )
                with self.assertRaises(RuntimeError):
                    verify_junit(path)
            path.write_text("<testsuite><testcase/></testsuite>", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                verify_junit(path)
            path.write_text("<testsuite>" + "<testcase/>" * 5 + "</testsuite>", encoding="utf-8")
            self.assertEqual(verify_junit(path), 5)

    def test_long_uri_is_redacted_before_password_fragment(self):
        uri = "mongodb://user:secret@127.0.0.1:1234/?authSource=admin"
        self.assertEqual(redact(uri + " secret", ["secret", uri]), "[REDACTED] [REDACTED]")

    def test_forced_child_failure_persists_only_bounded_sanitized_output(self):
        password, key = "secret-password-value", "long-keyfile-value" * 400
        uri = "mongodb://someone:unexpected-value@localhost:1234/?authSource=admin"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            receipt = {}
            diagnostics = Diagnostics(receipt, path, [password, key])
            diagnostics.stage("wait_ping")
            result = SimpleNamespace(
                returncode=2,
                stdout=key + " " + password,
                stderr="X" * 5000 + " " + uri,
            )
            with patch("scripts.check_mongodb.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "sanitized") as caught:
                    command(["child", password], data=key, diagnostics=diagnostics)
            saved = path.read_text(encoding="utf-8")
            for secret in (password, key, uri, "unexpected-value"):
                self.assertNotIn(secret, saved)
                self.assertNotIn(secret, str(caught.exception))
            failure = json.loads(saved)["last_child_failure"]
            self.assertEqual(failure["stage"], "wait_ping")
            self.assertEqual(failure["returncode"], 2)
            self.assertLessEqual(len(failure["stdout_tail"]), 4096)
            self.assertLessEqual(len(failure["stderr_tail"]), 4096)
            self.assertNotIn("arguments", failure)
            self.assertNotIn("stdin", failure)
