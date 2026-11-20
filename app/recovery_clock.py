"""Recorded activity relevant to offline recovery; deadlines may be in the future."""

ACTIVITY_FIELDS = {
    "users": ("created_at",),
    "sessions": ("created_at", "last_seen"),
    "collections": ("created_at",),
    "loans": ("requested_at", "approved_at", "returned_at", "cancelled_at"),
    "reservations": ("created_at", "closed_at"),
    "audit_events": ("timestamp",),
}


def activity_after(rows_for, timestamp):
    return any(
        type(value := row.get(field)) is int and value > timestamp
        for name, fields in ACTIVITY_FIELDS.items()
        for row in rows_for(name)
        for field in fields
    )
