"""Controller-side generators; importing this module needs no application code."""

from hypothesis import strategies as st

NAMES = st.sampled_from(("Straße", "STRASSE", "Σς", "ΣΣ", "ΐ", "A🎬", "Nom, prénom"))
ACTION = st.tuples(st.integers(0, 7), st.integers(0, 2), st.integers(0, 9), NAMES)
