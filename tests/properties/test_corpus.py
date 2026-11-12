"""Qualify the controller's actual eight-sequence corpus on a real JSON file."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from .harness import scenario_fixture
from .native import run_corpus, validate_corpus


def test_controller_export_is_reproducible_and_runs_eight_twelve_step_sequences(tmp_path):
    program = Path(__file__).with_name("export_corpus.py")
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for target in (first, second):
        subprocess.run(
            [sys.executable, str(program), "--output", str(target)], check=True, capture_output=True
        )
    assert first.read_bytes() == second.read_bytes()
    corpus = json.loads(first.read_text(encoding="utf-8"))
    assert len(validate_corpus(corpus)) == 8
    with (
        scenario_fixture() as fixture
    ):  # Deliberately prepopulated with another account/movie fixture.
        result = run_corpus(fixture.backend, corpus)
        assert result == {"private_steps": 48, "receipt_steps": 48, "sequences_completed": 8}
        revision = fixture.backend.query(lambda work: work.revision)
        invalid = copy.deepcopy(corpus)
        invalid["cases"][-1]["actions"][-1][0] = True
        with pytest.raises(ValueError, match="action"):
            run_corpus(fixture.backend, invalid)
        assert fixture.backend.query(lambda work: work.revision) == revision
