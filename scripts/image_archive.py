"""Verify the link between a Docker image, its saved bytes and Grype's report."""

import base64
import gzip
import hashlib
import json
import re
import stat
import tarfile
import zlib
from collections import Counter

SCANNER = (
    "anchore/grype:v0.121.2@sha256:6a36234da13f9faa327306ac0b3dab3405cfa2b4eb20c8f904125a8707863913"
)
SCANNER_VERSION = "0.121.2"
REPORT_NAME = "image-vulnerabilities.json"
CATALOGER = (
    "anchore/syft:v1.57.2@sha256:9c2563b5fb56e9f5e9cb5b6cf10b9ab0dc6a6d1c1b1e24165bcdf9c8c85a9584"
)
CATALOGER_VERSION = "1.57.2"
SBOM_NAME = "image-sbom.json"
SEVERITIES = ("Unknown", "Negligible", "Low", "Medium", "High", "Critical")
MAX_REPORT = 32 * 1024**2
MAX_METADATA = 4 * 1024**2
MAX_ARCHIVE = 4 * 1024**3
BLOCK_SIZE = 1024**2
MAX_TAR_ENTRIES = 4096

INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
CONFIG_TYPES = (
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
)
LAYER_TYPES = (
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def is_digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def digest_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _unique_keys(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Image evidence contains duplicate JSON keys")
    return value


def read_json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_keys)
    except (UnicodeError, RecursionError):
        raise ValueError("Image evidence contains invalid JSON") from None


def _file_size(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Image evidence is not a regular file")
    return info.st_size


def read_report(path):
    if _file_size(path) > MAX_REPORT:
        raise ValueError("Grype report is too large")
    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT + 1)
    if len(data) > MAX_REPORT:
        raise ValueError("Grype report is too large")
    return data


def _archive_size(path):
    size = _file_size(path)
    if not 0 < size <= MAX_ARCHIVE or size % tarfile.BLOCKSIZE:
        raise ValueError("Invalid Docker archive size")
    return size


def archive_sha256(path):
    _archive_size(path)
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


class _MetadataReader:
    """Limit even the hidden PAX and GNU metadata reads done by tarfile."""

    def __init__(self, stream):
        self.stream = stream
        self.remaining = 4 * MAX_METADATA

    def read(self, size=-1):
        if self.remaining is not None:
            if not 0 <= size <= MAX_METADATA or size > self.remaining:
                raise ValueError("Excessive Docker tar metadata")
            self.remaining -= size
        return self.stream.read(size)

    def seek(self, offset, whence=0):
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()

    def seekable(self):
        return self.stream.seekable()


def _inventory(archive, reader, size):
    members = {}
    end = 0
    for member in archive:
        name = member.name
        if (
            len(members) == MAX_TAR_ENTRIES
            or not re.fullmatch(r"[a-zA-Z0-9._/-]{1,255}", name)
            or {"", ".", ".."}.intersection(name.split("/"))
            or name in members
            or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
            or member.sparse is not None
            or member.size < 0
            or member.isdir()
            and member.size != 0
            or not end <= member.offset <= member.offset_data
        ):
            raise ValueError("Invalid or ambiguous Docker tar member")
        end = member.offset_data + (-(-member.size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE)
        if end > size:
            raise ValueError("Truncated Docker tar member")
        members[name] = member
    for name in members:
        parent, _, _ = name.rpartition("/")
        while parent:
            if parent in members and not members[parent].isdir():
                raise ValueError("Conflicting Docker tar paths")
            parent, _, _ = parent.rpartition("/")
    padding = size - end
    if not 2 * tarfile.BLOCKSIZE <= padding <= MAX_METADATA:
        raise ValueError("Invalid Docker tar padding")
    reader.seek(end)
    if reader.read(padding).strip(b"\0"):
        raise ValueError("Unexpected data after Docker tar members")
    return members


def _regular(members, name):
    if not isinstance(name, str) or name not in members or not members[name].isreg():
        raise ValueError("Docker archive is missing a required regular file")
    return members[name]


def _metadata(archive, member):
    if member.size > MAX_METADATA:
        raise ValueError("Oversized Docker JSON metadata")
    with archive.extractfile(member) as item:
        data = item.read()
    return data


def _blob_path(digest):
    if not is_digest(digest):
        raise ValueError("Invalid OCI SHA-256 descriptor")
    return "blobs/sha256/" + digest[7:]


def _descriptor_member(members, descriptor, media_types):
    if not isinstance(descriptor, dict) or descriptor.get("mediaType") not in media_types:
        raise ValueError("Unexpected OCI descriptor")
    member = _regular(members, _blob_path(descriptor.get("digest")))
    if type(descriptor.get("size")) is not int or member.size != descriptor["size"]:
        raise ValueError("OCI descriptor size does not match its blob")
    return member


def _verified_json_blob(archive, members, descriptor, media_types):
    member = _descriptor_member(members, descriptor, media_types)
    data = _metadata(archive, member)
    if digest_bytes(data) != descriptor["digest"]:
        raise ValueError("OCI descriptor digest does not match its blob")
    result = read_json(data)
    if (
        not isinstance(result, dict)
        or result.get("schemaVersion") != 2
        or result.get("mediaType", descriptor["mediaType"]) != descriptor["mediaType"]
    ):
        raise ValueError("Invalid OCI JSON blob")
    return result


def _selected_manifest(index, config):
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list) or any(not isinstance(d, dict) for d in descriptors):
        raise ValueError("Invalid OCI image index")
    runtime = [d for d in descriptors if d.get("platform", {}).get("os") != "unknown"]
    if len(runtime) != 1:
        raise ValueError("OCI export must identify exactly one runnable platform image")
    selected = runtime[0]
    platform = selected.get("platform", {})
    if any(
        not config.get(key) or config[key] != platform.get(key) for key in ("os", "architecture")
    ):
        raise ValueError("OCI platform does not match the archived image configuration")
    for other in descriptors:
        if other is selected:
            continue
        annotations = other.get("annotations", {})
        if (
            other["platform"].get("architecture") != "unknown"
            or annotations.get("vnd.docker.reference.type") != "attestation-manifest"
            or annotations.get("vnd.docker.reference.digest") != selected.get("digest")
        ):
            raise ValueError("Unexpected OCI index sibling")
    return selected


def _oci_manifest(archive, members, docker_id, legacy, config):
    layout = read_json(_metadata(archive, _regular(members, "oci-layout")))
    export_index = read_json(_metadata(archive, _regular(members, "index.json")))
    descriptors = export_index.get("manifests")
    if (
        layout.get("imageLayoutVersion") != "1.0.0"
        or export_index.get("schemaVersion") != 2
        or not isinstance(descriptors, list)
        or len(descriptors) != 1
        or not isinstance(descriptors[0], dict)
        or descriptors[0].get("digest") != docker_id
    ):
        raise ValueError("Docker identity does not match the exported OCI index")
    export_target = descriptors[0]
    if export_target.get("mediaType") in INDEX_TYPES:
        index = _verified_json_blob(archive, members, export_target, INDEX_TYPES)
        selected = _selected_manifest(index, config)
        for sibling in index["manifests"]:
            if sibling is not selected:
                _verified_json_blob(archive, members, sibling, MANIFEST_TYPES)
    else:
        selected = export_target
    manifest = _verified_json_blob(archive, members, selected, MANIFEST_TYPES)
    config_descriptor = manifest.get("config")
    config_member = _descriptor_member(members, config_descriptor, CONFIG_TYPES)
    if (
        config_member.name != legacy["Config"]
        or digest_bytes(_metadata(archive, config_member)) != config_descriptor["digest"]
    ):
        raise ValueError("OCI manifest and Docker archive disagree on image configuration")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or len(layers) != len(legacy["Layers"]):
        raise ValueError("OCI manifest and Docker archive disagree on layers")
    archive.fileobj.remaining = None
    for descriptor, path in zip(layers, legacy["Layers"], strict=True):
        layer = _descriptor_member(members, descriptor, LAYER_TYPES)
        if layer.name != path:
            raise ValueError("OCI manifest and Docker archive disagree on layer order")
        with archive.extractfile(layer) as item:
            actual = "sha256:" + hashlib.file_digest(item, "sha256").hexdigest()
        if actual != descriptor["digest"]:
            raise ValueError("OCI layer bytes do not match the manifest")
    return selected["digest"]


def _diff_id(stream, remaining):
    digest = hashlib.sha256()
    while data := stream.read(BLOCK_SIZE):
        remaining -= len(data)
        if remaining < 0:
            raise ValueError("Excessive uncompressed Docker layer data")
        digest.update(data)
    return "sha256:" + digest.hexdigest(), remaining


def _verify_legacy_layers(archive, members, layers, diff_ids):
    remaining = 2 * MAX_ARCHIVE
    for path, expected in zip(layers, diff_ids, strict=True):
        layer = _regular(members, path)
        with archive.extractfile(layer) as item:
            compressed = item.read(2) == b"\x1f\x8b"
            item.seek(0)
            if compressed:
                with gzip.GzipFile(fileobj=item) as expanded:
                    actual, remaining = _diff_id(expanded, remaining)
            else:
                actual, remaining = _diff_id(item, remaining)
        if actual != expected:
            raise ValueError("Docker image layer does not match its configuration")


def inspect_image_archive(path, docker_id):
    """Read the single image saved for the observed Docker identity."""
    if not is_digest(docker_id):
        raise ValueError("Invalid observed Docker identity")
    size = _archive_size(path)
    try:
        with path.open("rb") as stream:
            reader = _MetadataReader(stream)
            with tarfile.open(fileobj=reader, mode="r:") as archive:
                members = _inventory(archive, reader, size)
                manifest = read_json(_metadata(archive, _regular(members, "manifest.json")))
                if not isinstance(manifest, list) or len(manifest) != 1:
                    raise ValueError("Docker archive must contain exactly one image")
                legacy = manifest[0]
                config_bytes = _metadata(archive, _regular(members, legacy["Config"]))
                config_digest = digest_bytes(config_bytes)
                config = read_json(config_bytes)
                layers = legacy["Layers"]
                diff_ids = config["rootfs"]["diff_ids"]
                if (
                    not isinstance(layers, list)
                    or not isinstance(diff_ids, list)
                    or config["rootfs"].get("type") != "layers"
                    or not all(map(is_digest, diff_ids))
                    or len(layers) != len(diff_ids)
                ):
                    raise ValueError("Invalid Docker layer inventory")
                if docker_id == config_digest:
                    reader.remaining = None
                    _verify_legacy_layers(archive, members, layers, diff_ids)
                    kind, oci_manifest = "docker-config", None
                else:
                    oci_manifest = _oci_manifest(archive, members, docker_id, legacy, config)
                    kind = "oci-manifest" if oci_manifest == docker_id else "oci-index"
            stream.seek(0)
            sha256 = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except (
        tarfile.TarError,
        gzip.BadGzipFile,
        EOFError,
        OverflowError,
        RecursionError,
        zlib.error,
    ):
        raise ValueError("Malformed Docker archive") from None
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Malformed Docker archive metadata") from None
    return {
        "docker_image_id": docker_id,
        "archive_identity_kind": kind,
        "archive_sha256": sha256,
        "archive_config_digest": config_digest,
        "archive_oci_manifest_digest": oci_manifest,
    }


def _image_source(source, target, archive_config_digest):
    metadata = source[target]
    config = base64.b64decode(metadata["config"], validate=True)
    manifest = base64.b64decode(metadata["manifest"], validate=True)
    if len(config) > MAX_METADATA or len(manifest) > MAX_METADATA:
        raise ValueError("Oversized scanner image metadata")
    document = read_json(manifest)
    manifest_config = document["config"]
    manifest_digest = digest_bytes(manifest)
    if (
        source["type"] != "image"
        or metadata["userInput"] != "/image.tar"
        or metadata["imageID"] != archive_config_digest
        or digest_bytes(config) != archive_config_digest
        or metadata["manifestDigest"] != manifest_digest
        or document["schemaVersion"] != 2
        or manifest_config["digest"] != archive_config_digest
        or manifest_config["size"] != len(config)
    ):
        raise ValueError("Scanner evidence does not identify the saved image")
    return manifest_digest


def inspect_sbom(raw, archive_config_digest):
    """Check that Syft catalogued the inspected Docker archive."""
    if not 0 < len(raw) <= MAX_REPORT:
        raise ValueError("Invalid Syft inventory size")
    try:
        document = read_json(raw)
        manifest = _image_source(document["source"], "metadata", archive_config_digest)
        descriptor, artifacts = document["descriptor"], document["artifacts"]
        if (
            descriptor["name"] != "syft"
            or descriptor["version"] != CATALOGER_VERSION
            or not isinstance(artifacts, list)
            or not artifacts
        ):
            raise ValueError("Invalid Syft inventory or cataloger version")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Malformed Syft image evidence") from None
    return {
        "sbom_sha256": digest_bytes(raw),
        "sbom_config_digest": archive_config_digest,
        "sbom_manifest_digest": manifest,
        "sbom_components": len(artifacts),
        "sbom_cataloger": CATALOGER,
    }


def inspect_scan_report(raw, archive_config_digest):
    """Verify the exact JSON bytes and Grype's embedded reconstructed image metadata."""
    if not 0 < len(raw) <= MAX_REPORT:
        raise ValueError("Invalid Grype report size")
    try:
        report = read_json(raw)
        manifest_digest = _image_source(report["source"], "target", archive_config_digest)
        if not isinstance(report["matches"], list):
            raise ValueError("Grype report has no matches list")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Malformed Grype image evidence") from None
    return report, {
        "scan_report_sha256": digest_bytes(raw),
        "grype_config_digest": archive_config_digest,
        "grype_manifest_digest": manifest_digest,
    }


def scan_result(report):
    try:
        counts = dict(Counter(match["vulnerability"]["severity"] for match in report["matches"]))
        descriptor = report["descriptor"]
        database, version = descriptor["db"], descriptor["version"]
    except (KeyError, TypeError):
        raise ValueError("Scanner result is missing or malformed") from None
    if any(severity not in SEVERITIES for severity in counts) or not isinstance(database, dict):
        raise ValueError("Scanner result is missing or malformed")
    return {"severity_counts": counts, "database": database, "scanner_version": version}
