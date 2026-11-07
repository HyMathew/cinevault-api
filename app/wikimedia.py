"""Read-only Wikimedia client with fixed endpoints and a shared JSON cache."""

import json
import re
import time
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from .metadata import (
    QID,
    MetadataBusy,
    MetadataCache,
    MetadataUnavailable,
    label,
    normalize_film,
    normalize_poster,
    related_ids,
)
from .repository import unique_pairs
from .validation import is_unicode

WIKIDATA = "https://www.wikidata.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, params, agent):
    if url not in (WIKIDATA, COMMONS):
        raise MetadataUnavailable()
    request = urllib.request.Request(
        url + "?" + urlencode(params),
        headers={"User-Agent": agent, "Accept": "application/json", "Accept-Encoding": "identity"},
    )
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=4) as response:
            if (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                != "application/json"
            ):
                raise MetadataUnavailable()
            encoded = response.read(1024 * 1024 + 1)
        if len(encoded) > 1024 * 1024:
            raise MetadataUnavailable()
        value = json.loads(
            encoded,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(value, dict):
            raise MetadataUnavailable()
        if "error" in value:
            if isinstance(value["error"], dict) and value["error"].get("code") in (
                "maxlag",
                "ratelimited",
            ):
                raise MetadataBusy(60)
            raise MetadataUnavailable()
        return value
    except HTTPError as exc:
        if exc.code in (429, 503):
            retry = exc.headers.get("Retry-After", "60")
            raise MetadataBusy(
                int(retry) if retry.isascii() and retry.isdecimal() else 60
            ) from None
        raise MetadataUnavailable() from None
    except (URLError, OSError, ValueError, RecursionError):
        raise MetadataUnavailable() from None


class WikimediaClient:
    def __init__(self, path, agent, clock=time.time):
        if (
            not isinstance(agent, str)
            or not re.fullmatch(r"[\x20-\x7e]{20,200}", agent)
            or "https://" not in agent
        ):
            raise ValueError("An explicit application User-Agent with a contact URL is required")
        self.cache = MetadataCache(path, clock=clock)
        self.agent = agent
        self.clock = clock

    def remote(self, reserve, endpoint, **params):
        reserve()
        return request_json(endpoint, {**params, "maxlag": 5, "format": "json"}, self.agent)

    def search(self, query):
        if (
            not isinstance(query, str)
            or not is_unicode(query)
            or not 1 <= len(query.strip()) <= 100
            or any(ord(c) < 32 for c in query)
        ):
            raise ValueError("Invalid search query")
        query = " ".join(query.strip().split())
        literal = query.replace("\\", " ").replace('"', " ")

        def fetch(reserve):
            data = self.remote(
                reserve,
                WIKIDATA,
                action="query",
                list="search",
                srsearch='"' + literal + '" haswbstatement:P31=Q11424',
                srnamespace=0,
                srlimit=5,
            )
            query_result = data.get("query", {})
            if not isinstance(query_result, dict):
                raise MetadataUnavailable()
            rows = query_result.get("search", [])
            if not isinstance(rows, list):
                raise MetadataUnavailable()
            ids = list(
                dict.fromkeys(
                    row["title"]
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("title"), str)
                    and QID.fullmatch(row["title"])
                )
            )[:5]
            if not ids:
                return {"items": []}
            values = self.remote(
                reserve,
                WIKIDATA,
                action="wbgetentities",
                ids="|".join(ids),
                props="labels|descriptions",
                languages="fr|en|mul",
            ).get("entities", {})
            if not isinstance(values, dict):
                raise MetadataUnavailable()
            return {
                "items": [
                    {
                        "id": identity,
                        "title": label(values[identity])[:200],
                        "description": label(values[identity], "descriptions")[:4000],
                        "source_url": "https://www.wikidata.org/wiki/" + identity,
                    }
                    for identity in ids
                    if isinstance(values.get(identity), dict) and label(values[identity])
                ]
            }

        return self.cache.fetch("search:" + query.casefold(), fetch)

    def detail(self, identity):
        if not isinstance(identity, str) or not QID.fullmatch(identity):
            raise ValueError("Invalid entity identifier")

        def fetch(reserve):
            entities = self.remote(
                reserve,
                WIKIDATA,
                action="wbgetentities",
                ids=identity,
                props="labels|descriptions|claims|info",
                languages="fr|en|mul",
            ).get("entities", {})
            if not isinstance(entities, dict) or not isinstance(entities.get(identity), dict):
                raise MetadataUnavailable()
            entity = entities[identity]
            if entity.get("id") != identity or "Q11424" not in related_ids(entity, "P31", 50):
                raise MetadataUnavailable()
            ids = list(
                dict.fromkeys(
                    related_ids(entity, "P57", 5)
                    + related_ids(entity, "P161", 30)
                    + related_ids(entity, "P136", 30)
                )
            )
            labels = {}
            for offset in range(0, len(ids), 50):
                values = self.remote(
                    reserve,
                    WIKIDATA,
                    action="wbgetentities",
                    ids="|".join(ids[offset : offset + 50]),
                    props="labels",
                    languages="fr|en|mul",
                ).get("entities", {})
                if not isinstance(values, dict):
                    raise MetadataUnavailable()
                labels.update(
                    {qid: label(value) for qid, value in values.items() if isinstance(value, dict)}
                )
            film = normalize_film(entity, labels, int(self.clock()))
            film["poster"] = None
            if film["poster_filename"]:
                try:
                    data = self.remote(
                        reserve,
                        COMMONS,
                        action="query",
                        prop="imageinfo",
                        titles="File:" + film["poster_filename"],
                        iiprop="url|extmetadata|mime",
                        iiurlwidth=480,
                    )
                    query_result = data.get("query", {})
                    pages = query_result.get("pages", {}) if isinstance(query_result, dict) else {}
                    if isinstance(pages, dict):
                        for page in pages.values():
                            if not isinstance(page, dict):
                                continue
                            info = page.get("imageinfo", [])
                            if isinstance(info, list) and info:
                                film["poster"] = normalize_poster(film["poster_filename"], info[0])
                                break
                except MetadataBusy:
                    # Preserve provider backoff rather than silently consuming more requests.
                    raise
                except MetadataUnavailable:
                    film["warnings"].append(
                        "Affiche indisponible ; les métadonnées restent consultables."
                    )
                if film["poster"] is None:
                    film["warnings"].append(
                        "Affiche non proposée : provenance ou conditions insuffisantes, ou fichier indisponible."
                    )
            return {"film": film}

        return self.cache.fetch("detail:" + identity, fetch)
