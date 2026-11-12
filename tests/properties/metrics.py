"""Counts of completed generated examples, without payloads or credentials."""

COUNTS = {}


def record(name, steps=1):
    value = COUNTS.setdefault(name, {"examples_completed": 0, "steps_completed": 0})
    value["examples_completed"] += 1
    value["steps_completed"] += steps
