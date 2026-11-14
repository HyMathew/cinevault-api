"""Four explicit C4 mutations in an immutable export; the original twelve stay separate."""

import mutation_gate

CASES = (
    (
        "circulation-fifo-bypassed",
        "app/circulation_model.py",
        "return is_head if owned else free > reserved",
        "return free > 0",
        "tests/properties/test_circulation_sequences.py::test_fifo_tail_cannot_take_head_slot_regression",
        "FIFO tail options must stay empty",
    ),
    (
        "circulation-expiry-exclusive",
        "app/persistence/json_circulation.py",
        'lambda row: row["status"] == "waiting" and row["expires_at"] <= timestamp, batch_size',
        'lambda row: row["status"] == "waiting" and row["expires_at"] < timestamp, batch_size',
        "tests/properties/test_circulation_sequences.py::test_expiration_exact_and_cancelled_history_regression",
        "Reservation history must match the plan",
    ),
    (
        "circulation-tenth-pending-rejected",
        "app/services/circulation.py",
        'if work.circulation.count_loans(user["id"], "pending") >= 10:',
        'if work.circulation.count_loans(user["id"], "pending") >= 9:',
        "tests/properties/test_circulation_sequences.py::test_pending_active_waiting_real_quota_regression",
        "Circulation outcome",
    ),
    (
        "circulation-receipt-response-rewritten",
        "app/services/circulation.py",
        'body=cached["response"],',
        'body={**cached["response"], "status": "active"},',
        "tests/properties/test_circulation_sequences.py::test_historical_receipt_and_exact_ttl_regression",
        "Closed entity replay must remain historical",
    ),
)


def main():
    # Reuse archive/provenance/fail-closed classification, not a second runner.
    # This assignment is confined to this process and never edits the P1-P3 file.
    mutation_gate.CASES = CASES
    mutation_gate.main()


if __name__ == "__main__":
    main()
