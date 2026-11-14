"""Generate P4 plans on the controller; the interpreter needs only app + stdlib."""

import argparse
import hashlib
import json
from pathlib import Path

from hypothesis import Phase, given, seed, settings
from hypothesis import strategies as st


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20261113)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output must be a new file")
    cases = []

    @seed(args.seed)
    @settings(max_examples=2, deadline=None, database=None, phases=(Phase.generate,))
    @given(
        st.permutations((0, 1, 3)),
        st.lists(st.booleans(), min_size=3, max_size=3),
        st.sampled_from(("none", "before", "at", "after")),
        st.permutations(tuple(range(11))),
        st.integers(0, 4),
        st.sampled_from(("loan", "reservation")),
        st.sampled_from(("before", "at", "after")),
    )
    def generate(order, removed, expiry, films, returned, kind, boundary):
        cases.extend(
            [
                {
                    "domain": "fifo",
                    "order": list(order),
                    "cancelled": [
                        actor for actor, remove in zip(order, removed, strict=True) if remove
                    ],
                    "expiry": expiry,
                },
                {"domain": "quotas", "order": list(films), "returned_slot": returned},
                {"domain": "receipts", "kind": kind, "boundary": boundary},
            ]
        )

    generate()
    cases.extend([{"domain": "maintenance"}, {"domain": "expired_receipt"}])
    value = {"format": 1, "domain": "circulation", "seed": args.seed, "cases": cases}
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as handle:
        handle.write(data)
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
