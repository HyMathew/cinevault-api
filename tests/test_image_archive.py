"""The scanner must describe the exact platform image selected from Docker save."""

import base64
import gzip
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile

import pytest

from scripts import check_image
from scripts.image_archive import inspect_image_archive, inspect_scan_report

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


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


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")


def blob(data):
    return "blobs/sha256/" + digest(data).split(":")[1]


def descriptor(media_type, data, **metadata):
    return {"mediaType": media_type, "digest": digest(data), "size": len(data), **metadata}


def write_tar(path, entries):
    with tarfile.open(path, "w") as archive:
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


@pytest.fixture
def archive_data():
    layer = b"synthetic layer data"
    config = encoded(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [digest(layer)]},
        }
    )
    layer_gzip = gzip.compress(layer, mtime=0)
    platform = {"os": "linux", "architecture": "amd64"}
    runtime = encoded(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": descriptor(CONFIG, config),
            "layers": [descriptor(LAYER, layer_gzip)],
        }
    )
    attestation = encoded({"schemaVersion": 2, "mediaType": MANIFEST})
    index = encoded(
        {
            "schemaVersion": 2,
            "mediaType": INDEX,
            "manifests": [
                descriptor(MANIFEST, runtime, platform=platform),
                descriptor(
                    MANIFEST,
                    attestation,
                    platform={"os": "unknown", "architecture": "unknown"},
                    annotations={
                        "vnd.docker.reference.type": "attestation-manifest",
                        "vnd.docker.reference.digest": digest(runtime),
                    },
                ),
            ],
        }
    )
    return (
        index,
        runtime,
        config,
        layer,
        {
            "oci-layout": encoded({"imageLayoutVersion": "1.0.0"}),
            "index.json": encoded(
                {"schemaVersion": 2, "mediaType": INDEX, "manifests": [descriptor(INDEX, index)]}
            ),
            "manifest.json": encoded([{"Config": blob(config), "Layers": [blob(layer_gzip)]}]),
            blob(index): index,
            blob(runtime): runtime,
            blob(config): config,
            blob(layer_gzip): layer_gzip,
            blob(attestation): attestation,
        },
    )


def scan_report(config, *, severity=None):
    # The manifest reconstructed by Grype intentionally differs from the OCI manifest.
    manifest = encoded(
        {"schemaVersion": 2, "config": {"digest": digest(config), "size": len(config)}}
    )
    return {
        "source": {
            "type": "image",
            "target": {
                "userInput": "/image.tar",
                "imageID": digest(config),
                "config": base64.b64encode(config).decode("ascii"),
                "manifestDigest": digest(manifest),
                "manifest": base64.b64encode(manifest).decode("ascii"),
            },
        },
        "descriptor": {"version": "0.121.2", "db": {"status": {"valid": True}}},
        "matches": [{"vulnerability": {"severity": severity}}] if severity else [],
    }


def test_oci_index_links_docker_id_to_exact_legacy_config(tmp_path, archive_data):
    path = tmp_path / "image.tar"
    index, runtime, config, _, members = archive_data
    write_tar(path, members.items())
    evidence = inspect_image_archive(path, digest(index))
    assert evidence == {
        "docker_image_id": digest(index),
        "archive_identity_kind": "oci-index",
        "archive_sha256": digest(path.read_bytes()),
        "archive_config_digest": digest(config),
        "archive_oci_manifest_digest": digest(runtime),
    }
    raw_report = encoded(scan_report(config))
    report, attestation = inspect_scan_report(raw_report, digest(config))
    assert report["matches"] == []
    assert attestation["scan_report_sha256"] == digest(raw_report)
    assert attestation["grype_config_digest"] == digest(config)
    assert attestation["grype_manifest_digest"] != evidence["archive_oci_manifest_digest"]


@pytest.mark.parametrize("compressed", [False, True])
def test_classic_config_hash_and_layer_diffids(tmp_path, archive_data, compressed):
    path = tmp_path / "image.tar"
    _, _, config, layer, _ = archive_data
    config_path = digest(config).split(":")[1] + ".json"
    legacy = {
        "manifest.json": encoded([{"Config": config_path, "Layers": ["a/layer.tar"]}]),
        config_path: config,
        "a/layer.tar": gzip.compress(layer) if compressed else layer,
    }
    write_tar(path, legacy.items())
    receipt = inspect_image_archive(path, digest(config))
    assert receipt["archive_identity_kind"] == "docker-config"
    assert receipt["archive_oci_manifest_digest"] is None
    legacy["a/layer.tar"] += b"different layer"
    write_tar(path, legacy.items())
    with pytest.raises(ValueError):
        inspect_image_archive(path, digest(config))


def test_oci_archive_accepts_direct_platform_manifest_as_docker_id(tmp_path, archive_data):
    path = tmp_path / "image.tar"
    _, runtime, config, _, members = archive_data
    members["index.json"] = encoded(
        {"schemaVersion": 2, "mediaType": INDEX, "manifests": [descriptor(MANIFEST, runtime)]}
    )
    write_tar(path, members.items())
    receipt = inspect_image_archive(path, digest(runtime))
    assert receipt["archive_identity_kind"] == "oci-manifest"
    assert receipt["archive_oci_manifest_digest"] == receipt["docker_image_id"] == digest(runtime)
    assert receipt["archive_config_digest"] == digest(config)


@pytest.mark.parametrize(
    "defect",
    ["duplicate", "oversized_manifest", "wrong_index", "wrong_config", "wrong_layer", "tail"],
)
def test_invalid_or_ambiguous_oci_archives_are_refused(tmp_path, archive_data, defect):
    path = tmp_path / "image.tar"
    index, runtime, config, _, members = archive_data
    if defect == "oversized_manifest":
        members["manifest.json"] = b" " * (5 << 20)
    if defect == "wrong_index":
        members[blob(index)] += b" "
    if defect == "wrong_config":
        members[blob(config)] += b" "
    if defect == "wrong_layer":
        path_to_layer = json.loads(runtime)["layers"][0]["digest"].replace(":", "/")
        data = members["blobs/" + path_to_layer]
        members["blobs/" + path_to_layer] = b"x" + data[1:]
    pairs = list(members.items())
    if defect == "duplicate":
        pairs.append(("manifest.json", members["manifest.json"]))
    write_tar(path, pairs)
    if defect == "tail":
        with path.open("ab") as output:
            output.write(b"extra unaccounted content")
    with pytest.raises(ValueError):
        inspect_image_archive(path, digest(index))


@pytest.mark.parametrize("defect", ["wrong_expected_config", "lying_image_id", "changed_manifest"])
def test_grype_attestation_must_match_source_config(archive_data, defect):
    _, _, config, _, _ = archive_data
    expected = digest(config)
    report = scan_report(config)
    if defect == "wrong_expected_config":
        expected = digest(b"another image")
    if defect == "lying_image_id":
        report["source"]["target"]["config"] = base64.b64encode(b"another image").decode("ascii")
    if defect == "changed_manifest":
        report["source"]["target"]["manifestDigest"] = digest(b"another manifest")
    with pytest.raises(ValueError):
        inspect_scan_report(encoded(report), expected)


@pytest.mark.parametrize("result", ["success", "high", "archive_changed"])
def test_check_binds_exact_scan_bytes_without_promoting_failed_scans(
    tmp_path, archive_data, result
):
    index, _, config, _, members = archive_data
    path = tmp_path / "image.tar"
    write_tar(path, members.items())
    evidence = inspect_image_archive(path, digest(index))
    raw_report = (
        encoded(scan_report(config, severity="High" if result == "high" else None)) + b"\r\n"
    )
    scan = subprocess.CompletedProcess([], 2 if result == "high" else 0, raw_report, b"")
    receipt = {"scan_passed": False}
    if result == "archive_changed":
        # Keep the file size and format valid so the mismatch is caught by SHA-256.
        with path.open("r+b") as archive:
            archive.seek(-1, os.SEEK_END)
            archive.write(b"x")
        with pytest.raises(ValueError, match="changed during"):
            check_image.record_scan(receipt, tmp_path, path, evidence, scan)
        assert "scan_provenance" not in receipt
    elif result == "high":
        with pytest.raises(RuntimeError, match="Image scan blocked"):
            check_image.record_scan(receipt, tmp_path, path, evidence, scan)
    else:
        check_image.record_scan(receipt, tmp_path, path, evidence, scan)
    assert (tmp_path / "image-vulnerabilities.json").read_bytes() == raw_report
    assert receipt["scan_passed"] is (result == "success")
    if result != "archive_changed":
        assert receipt["scan_provenance"]["scan_report_sha256"] == digest(raw_report)
