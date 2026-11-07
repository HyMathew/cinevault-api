"""Bounded, optional public metadata; independent of the business store lock."""

import copy
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import portalocker

from .metadata_model import QID
from .repository import unique_pairs
from .storage_v2 import _replace
from .validation import is_unicode

TTL = 86400
GRACE = 7 * 86400


class MetadataUnavailable(Exception):
    pass


class MetadataBusy(MetadataUnavailable):
    def __init__(self, retry_after=60):
        self.retry_after = max(1, min(3600, int(retry_after)))
        super().__init__("Metadata temporarily unavailable")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(value, limit=240):
    if not isinstance(value, str) or not is_unicode(value):
        return ""
    parser = PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())[:limit]


def claim_values(entity, property):
    if not isinstance(entity.get("claims"), dict):
        return []
    claims = entity["claims"].get(property, [])
    if not isinstance(claims, list):
        return []
    preferred = any(isinstance(c, dict) and c.get("rank") == "preferred" for c in claims)
    return [
        c["mainsnak"]["datavalue"]["value"]
        for c in claims
        if isinstance(c, dict)
        and c.get("rank") != "deprecated"
        and (not preferred or c.get("rank") == "preferred")
        and isinstance(c.get("mainsnak"), dict)
        and c["mainsnak"].get("snaktype") == "value"
        and isinstance(c["mainsnak"].get("datavalue"), dict)
        and "value" in c["mainsnak"]["datavalue"]
    ]


def related_ids(entity, property, limit):
    return list(
        dict.fromkeys(
            value["id"]
            for value in claim_values(entity, property)
            if isinstance(value, dict)
            and isinstance(value.get("id"), str)
            and QID.fullmatch(value["id"])
        )
    )[:limit]


def label(entity, field="labels"):
    values = entity.get(field, {})
    if not isinstance(values, dict):
        return ""
    for language in ("fr", "en", "mul"):
        value = values.get(language, {})
        if (
            isinstance(value, dict)
            and isinstance(value.get("value"), str)
            and is_unicode(value["value"])
        ):
            return value["value"].strip()
    return ""


def normalize_film(entity, labels, timestamp):
    if (
        not isinstance(entity, dict)
        or not isinstance(entity.get("id"), str)
        or not QID.fullmatch(entity["id"])
        or "Q11424" not in related_ids(entity, "P31", 50)
    ):
        raise MetadataUnavailable()
    revision = entity.get("lastrevid")
    if type(revision) is not int or revision < 1 or not label(entity):
        raise MetadataUnavailable()
    warnings = []

    def text(value, limit, field):
        if not isinstance(value, str) or not is_unicode(value):
            return ""
        value = value.strip()
        if len(value) > limit:
            warnings.append(field + " raccourci pour respecter les limites.")
        return value[:limit]

    def names(property, maximum, length):
        ids = related_ids(entity, property, maximum + 1)
        if len(ids) > maximum:
            warnings.append(
                "Liste " + property + " limitée aux " + str(maximum) + " premiers éléments."
            )
        result = []
        for identity in ids[:maximum]:
            value = text(labels.get(identity, ""), length, property)
            if value and value.casefold() not in [entry.casefold() for entry in result]:
                result.append(value)
        if len(result) < min(len(ids), maximum):
            warnings.append("Libellés " + property + " partiellement manquants.")
        return result

    directors = names("P57", 5, 120)
    director = text(" / ".join(directors), 120, "Réalisation")
    years = []
    for value in claim_values(entity, "P577"):
        if (
            not isinstance(value, dict)
            or type(value.get("precision")) is not int
            or value["precision"] < 9
        ):
            continue
        match = (
            re.match(r"^\+([0-9]{4})-", value.get("time", ""))
            if isinstance(value.get("time"), str)
            else None
        )
        if (
            match
            and 1888 <= int(match[1]) <= 2100
            and value.get("calendarmodel", "").rsplit("/", 1)[-1] in ("", "Q1985727")
        ):
            years.append(int(match[1]))
    if not years:
        warnings.append("Année à renseigner.")
    if not director:
        warnings.append("Réalisation à renseigner.")
    if len(set(years)) > 1:
        warnings.append("Plusieurs années de sortie ; vérifier la première année proposée.")
    posters = claim_values(entity, "P3383")
    poster = next(
        (
            value
            for value in posters
            if isinstance(value, str) and is_unicode(value) and 1 <= len(value) <= 240
        ),
        None,
    )
    return {
        "id": entity["id"],
        "title": text(label(entity), 200, "Titre"),
        "year": min(years) if years else None,
        "director": director,
        "actors": names("P161", 30, 120),
        "genres": names("P136", 30, 80),
        "description": text(label(entity, "descriptions"), 4000, "Description"),
        "poster_filename": poster,
        "revision": revision,
        "retrieved_at": timestamp,
        "source_url": "https://www.wikidata.org/wiki/" + entity["id"],
        "warnings": list(dict.fromkeys(warnings)),
    }


def normalize_poster(filename, info):
    if (
        not isinstance(filename, str)
        or not is_unicode(filename)
        or not 1 <= len(filename) <= 240
        or not isinstance(info, dict)
    ):
        return None
    url = info.get("thumburl", "")
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc not in ("upload.wikimedia.org", "thumb.wikimedia.org")
        or not parsed.path.startswith("/wikipedia/commons/")
        or parsed.fragment
    ):
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) > 3 or any(
        key not in ("utm_source", "utm_campaign", "utm_content") for key, value in query
    ):
        return None
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if info.get("thumbmime", info.get("mime")) not in ("image/jpeg", "image/png", "image/webp"):
        return None
    if not re.search(r"\.(?:jpe?g|png|webp)$", parsed.path, re.I):
        return None
    metadata = info.get("extmetadata", {})
    if not isinstance(metadata, dict):
        return None

    def value(key):
        entry = metadata.get(key, {})
        return plain(entry.get("value", ""), 1000) if isinstance(entry, dict) else ""

    license = value("LicenseShortName")
    licenses = {
        "CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
        "CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
        "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
        "CC BY 3.0": "https://creativecommons.org/licenses/by/3.0/",
        "CC BY-SA 3.0": "https://creativecommons.org/licenses/by-sa/3.0/",
        "Public domain": "https://commons.wikimedia.org/wiki/Commons:Public_domain",
    }
    if license not in licenses or value("Restrictions") or not value("Artist"):
        return None
    license_url = value("LicenseUrl")
    if license != "Public domain" and license_url.rstrip("/") != licenses[license].rstrip("/"):
        return None
    return {
        "image_url": url,
        "source_url": "https://commons.wikimedia.org/wiki/File:"
        + quote(filename.replace(" ", "_"), safe=""),
        "artist": value("Artist")[:240],
        "license": license,
        "license_url": licenses[license],
        "attribution_required": value("AttributionRequired").lower() != "false",
    }


def validated_poster(value):
    """Check cached links again before exposing them to public templates."""
    fields = {"image_url", "source_url", "artist", "license", "license_url", "attribution_required"}
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["attribution_required"]) is not bool
    ):
        return None
    if any(not isinstance(value[key], str) for key in fields - {"attribution_required"}):
        return None
    try:
        source = urlsplit(value["source_url"])
        image = urlsplit(value["image_url"])
    except ValueError:
        return None
    if (
        source.scheme != "https"
        or source.netloc != "commons.wikimedia.org"
        or source.query
        or source.fragment
        or not source.path.startswith("/wiki/File:")
    ):
        return None
    filename = unquote(source.path[len("/wiki/File:") :])
    extension = image.path.rsplit(".", 1)[-1].lower()
    mime = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(extension)
    checked = normalize_poster(
        filename,
        {
            "thumburl": value["image_url"],
            "thumbmime": mime,
            "extmetadata": {
                key: {"value": text}
                for key, text in {
                    "Artist": value["artist"],
                    "LicenseShortName": value["license"],
                    "LicenseUrl": value["license_url"],
                    "AttributionRequired": "true" if value["attribution_required"] else "false",
                }.items()
            },
        },
    )
    return checked if checked == value else None


class MetadataCache:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def read(self, now):
        if not self.path.exists():
            return {
                "version": 1,
                "window_start": now,
                "count": 0,
                "blocked_until": 0,
                "entries": {},
            }
        with self.path.open("rb") as stream:
            encoded = stream.read(8 * 1024 * 1024 + 1)
        if len(encoded) > 8 * 1024 * 1024:
            raise MetadataUnavailable()
        value = json.loads(encoded, object_pairs_hook=unique_pairs)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "window_start", "count", "blocked_until", "entries"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise MetadataUnavailable()
        if (
            any(
                type(value[k]) is not int or value[k] < 0
                for k in ("window_start", "count", "blocked_until")
            )
            or value["count"] > 20
            or value["window_start"] > now
        ):
            raise MetadataUnavailable()
        if not isinstance(value["entries"], dict) or len(value["entries"]) > 100:
            raise MetadataUnavailable()
        for key, entry in value["entries"].items():
            if (
                not re.fullmatch("[0-9a-f]{64}", key)
                or not isinstance(entry, dict)
                or set(entry) != {"stored_at", "expires_at", "stale_until", "value"}
            ):
                raise MetadataUnavailable()
            if (
                any(type(entry[k]) is not int for k in ("stored_at", "expires_at", "stale_until"))
                or not 0 <= entry["stored_at"] <= now
                or entry["expires_at"] != entry["stored_at"] + TTL
                or entry["stale_until"] != entry["expires_at"] + GRACE
            ):
                raise MetadataUnavailable()
            self.check_value(entry["value"])
        return value

    @staticmethod
    def check_value(value):
        if (
            not isinstance(value, dict)
            or "cache" in value
            or len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32768
        ):
            raise MetadataUnavailable()

    @staticmethod
    def result(entry, status, hit):
        return {
            **copy.deepcopy(entry["value"]),
            "cache": {"status": status, "stored_at": entry["stored_at"], "hit": hit},
        }

    @staticmethod
    def reserve(state, now, count=True):
        if now < state["window_start"]:
            raise MetadataUnavailable()
        if state["window_start"] + 60 <= now:
            state.update(window_start=now, count=0)
        if state["blocked_until"] > now:
            raise MetadataBusy(state["blocked_until"] - now)
        if state["count"] >= 20:
            raise MetadataBusy(state["window_start"] + 60 - now)
        if count:
            state["count"] += 1

    def peek(self, key):
        return self.peek_many([key]).get(key)

    def peek_many(self, keys):
        try:
            now = int(self.clock())
            if not self.path.exists():
                return {}
            with portalocker.Lock(str(self.path) + ".lock", mode="a", timeout=2):
                entries = self.read(now)["entries"]
                result = {}
                for key in set(keys):
                    entry = entries.get(hashlib.sha256(key.encode("utf-8")).hexdigest())
                    if entry and entry["expires_at"] > now:
                        result[key] = copy.deepcopy(entry["value"])
                return result
        except (
            OSError,
            ValueError,
            RecursionError,
            portalocker.exceptions.LockException,
            MetadataUnavailable,
        ):
            return {}

    def fetch(self, key, fetch):
        try:
            now = int(self.clock())
            if now < 0:
                raise MetadataUnavailable()
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            with portalocker.Lock(str(self.path) + ".lock", mode="a", timeout=2):
                state = self.read(now)
                entry = state["entries"].get(digest)
                if entry and entry["expires_at"] > now:
                    return self.result(entry, "fresh", True)
                try:
                    self.reserve(state, now, count=False)

                    def reserve_request():
                        self.reserve(state, int(self.clock()))
                        # Persist a consumed slot before I/O, including if a worker later dies.
                        _replace(
                            self.path,
                            (json.dumps(state, ensure_ascii=False, allow_nan=False) + "\n").encode(
                                "utf-8"
                            ),
                        )

                    value = fetch(reserve_request)
                    self.check_value(value)
                    completed = int(self.clock())
                    if completed < now:
                        raise MetadataUnavailable()
                    entry = {
                        "stored_at": completed,
                        "expires_at": completed + TTL,
                        "stale_until": completed + TTL + GRACE,
                        "value": value,
                    }
                    state["entries"][digest] = entry
                    state["entries"] = {
                        key: item
                        for key, item in sorted(
                            state["entries"].items(),
                            key=lambda pair: pair[1]["stored_at"],
                            reverse=True,
                        )
                        if item["stale_until"] > now
                    }
                    state["entries"] = dict(list(state["entries"].items())[:100])
                    return self.result(entry, "fresh", False)
                except MetadataUnavailable as exc:
                    if isinstance(exc, MetadataBusy):
                        state["blocked_until"] = max(
                            state["blocked_until"], max(now, int(self.clock())) + exc.retry_after
                        )
                    if entry and entry["stale_until"] > now:
                        return self.result(entry, "stale", True)
                    raise
                finally:
                    _replace(
                        self.path,
                        (json.dumps(state, ensure_ascii=False, allow_nan=False) + "\n").encode(
                            "utf-8"
                        ),
                    )
        except portalocker.exceptions.LockException:
            raise MetadataBusy(2) from None
        except (OSError, ValueError, RecursionError):
            raise MetadataUnavailable() from None
