"""Generate a bounded reproducible corpus on the controller, without database I/O."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from hypothesis import Phase, given, seed, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from properties.corpus import PRIVATE_PREFIX, RECEIPT_PREFIX  # noqa: E402
from properties.generators import ACTION  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20261112)
    args = parser.parse_args()
    cases = []

    @seed(args.seed)
    @settings(max_examples=4, deadline=None, database=None, phases=(Phase.generate,))
    @given(st.lists(ACTION, min_size=8, max_size=8), st.lists(ACTION, min_size=4, max_size=4))
    def generate(private, receipts):
        cases.append({"domain": "private", "actions": PRIVATE_PREFIX + private})
        cases.append({"domain": "receipts", "actions": RECEIPT_PREFIX + receipts})

    generate()
    value = {"format": 1, "generator": "Hypothesis-controller", "seed": args.seed, "cases": cases}
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(
        json.dumps(
            {
                "cases_generated": len(cases),
                "sha256": hashlib.sha256(data).hexdigest(),
                "native_executed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
