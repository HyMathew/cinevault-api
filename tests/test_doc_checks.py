from scripts.check_docs import markdown_errors


def test_documentation_gate_refuses_missing_link_and_accepts_existing_file(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "[Guide](docs/guide.md) [External](https://example.org/absent) # [Anchor](#section)",
        encoding="utf-8",
    )
    errors = markdown_errors(tmp_path)
    assert len(errors) == 1 and "docs/guide.md" in errors[0]
    (tmp_path / "docs/guide.md").write_text("# Guide", encoding="utf-8")
    assert markdown_errors(tmp_path) == []


def test_documentation_gate_refuses_outside_link_and_invalid_utf8(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("[Outside](../outside.md)", encoding="utf-8")
    (tmp_path / "docs/bad.md").write_bytes(b"\xff")
    errors = markdown_errors(tmp_path)
    assert len(errors) == 2
    assert any("outside" in error for error in errors)
    assert any("UTF-8" in error for error in errors)
