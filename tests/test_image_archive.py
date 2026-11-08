"""A Linux scanner must read its archive without gaining root or write access."""

import os
import stat

from scripts import check_image


def test_export_keeps_private_parent_and_read_only_archive(monkeypatch, tmp_path):
    if os.name == "posix":
        tmp_path.chmod(0o700)
    parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    archive = tmp_path / "image.tar"
    identity = "sha256:" + "a" * 64
    calls = []

    def docker_export(arguments):
        calls.append(arguments)
        archive.write_bytes(b"synthetic image archive")
        archive.chmod(0o600)
        return ""

    monkeypatch.setattr(check_image, "command", docker_export)
    exporter = getattr(check_image, "export_image_archive", None)
    assert callable(exporter), "Archive export must establish scanner-readable permissions"
    exporter(identity, archive)
    assert calls == [["docker", "image", "save", "--output", str(archive), identity]]
    assert archive.read_bytes() == b"synthetic image archive"
    mode = stat.S_IMODE(archive.stat().st_mode)
    assert mode & stat.S_IROTH
    assert not mode & 0o222
    assert stat.S_IMODE(tmp_path.stat().st_mode) == parent_mode
