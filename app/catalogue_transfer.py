"""Bounded catalogue-only formats, independent of private account data."""

import csv
import io
import json
import uuid
from collections import defaultdict

from .catalogue_query import movie_identity_key
from .repository import unique_pairs
from .services.catalogue_model import COPY_FIELDS, MOVIE_FIELDS, copy_data, movie_data
from .validation import ValidationError

COLUMNS = (
    "movie_id",
    "copy_id",
    "title",
    "year",
    "director",
    "actors",
    "genres",
    "synopsis",
    "support",
    "location",
    "condition",
)
EXTENDED_COLUMNS = (*COLUMNS, "external_source")
MAX_MOVIES = 1000
MAX_COPIES = 5000


class ImportConflict(Exception):
    pass


def invalid():
    raise ValidationError({"file": "Catalogue invalide ou limites dépassées."})


def identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            invalid()
    except (ValueError, AttributeError):
        invalid()
    return value


def load_json(value):
    try:
        return json.loads(
            value,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError):
        invalid()


def normalize(content):
    if isinstance(content, str):
        content = load_json(content)
    if (
        not isinstance(content, dict)
        or set(content) != {"format_version", "movies"}
        or type(content["format_version"]) is not int
        or content["format_version"] != 1
        or not isinstance(content["movies"], list)
        or len(content["movies"]) > MAX_MOVIES
    ):
        invalid()
    result = []
    ids, keys = set(), set()
    count = 0
    for item in content["movies"]:
        if (
            not isinstance(item, dict)
            or not set((*MOVIE_FIELDS, "copies")).issubset(item)
            or set(item) - set((*MOVIE_FIELDS, "copies", "genres", "id", "external_source"))
            or not isinstance(item["copies"], list)
        ):
            invalid()
        movie = movie_data(item)
        if "id" in item:
            movie["id"] = identifier(item["id"])
            if movie["id"] in ids:
                invalid()
            ids.add(movie["id"])
        key = movie_identity_key(movie)
        if key in keys:
            invalid()
        keys.add(key)
        copies = []
        count += len(item["copies"])
        if count > MAX_COPIES:
            invalid()
        for value in item["copies"]:
            if (
                not isinstance(value, dict)
                or set(value) - set((*COPY_FIELDS, "id"))
                or not set(COPY_FIELDS).issubset(value)
            ):
                invalid()
            entry = copy_data({k: value[k] for k in COPY_FIELDS})
            if "id" in value:
                entry["id"] = identifier(value["id"])
                if entry["id"] in ids:
                    invalid()
                ids.add(entry["id"])
            copies.append(entry)
        result.append({**movie, "copies": copies})
    return result


def parse_csv(content):
    if not isinstance(content, str) or "\x00" in content:
        invalid()
    try:
        reader = csv.reader(io.StringIO(content.removeprefix("\ufeff"), newline=""), strict=True)
        columns = tuple(next(reader))
        if columns not in (COLUMNS, EXTENDED_COLUMNS):
            invalid()
        grouped = {}
        for index, values in enumerate(reader):
            if index >= MAX_MOVIES + MAX_COPIES or len(values) != len(columns):
                invalid()
            row = dict(zip(columns, values))
            if not row["year"].isascii() or not row["year"].isdigit():
                invalid()
            metadata = {
                "title": row["title"],
                "year": int(row["year"]),
                "director": row["director"],
                "actors": load_json(row["actors"]),
                "genres": load_json(row["genres"]),
                "synopsis": row["synopsis"],
            }
            if row.get("external_source", ""):
                metadata["external_source"] = load_json(row["external_source"])
            key = (
                ("id", row["movie_id"])
                if row["movie_id"]
                else (
                    "natural",
                    row["title"].strip().casefold(),
                    metadata["year"],
                    row["director"].strip().casefold(),
                )
            )
            if key not in grouped:
                grouped[key] = {**metadata, "copies": []}
                if row["movie_id"]:
                    grouped[key]["id"] = row["movie_id"]
            elif any(grouped[key][k] != v for k, v in metadata.items()):
                invalid()
            if any(row[k] for k in ("copy_id", "support", "location", "condition")):
                entry = {k: row[k] for k in COPY_FIELDS}
                if row["copy_id"]:
                    entry["id"] = row["copy_id"]
                grouped[key]["copies"].append(entry)
        return {"format_version": 1, "movies": list(grouped.values())}
    except (csv.Error, StopIteration, UnicodeError, ValueError, OverflowError):
        invalid()


def export_document(movies, copies):
    by_movie = defaultdict(list)
    for entry in sorted(copies, key=lambda row: row["id"]):
        by_movie[entry["movie_id"]].append({field: entry[field] for field in ("id", *COPY_FIELDS)})
    return {
        "format_version": 1,
        "movies": [
            {
                **{
                    k: movie[k]
                    for k in ("id", *MOVIE_FIELDS, "genres", "external_source")
                    if k in movie
                },
                "copies": by_movie[movie["id"]],
            }
            for movie in sorted(movies, key=lambda row: row["id"])
        ],
    }


def csv_safe(value):
    value = str(value)
    if value[:1] in ("\t", "\r", "\n") or value.lstrip(" \t\r\n")[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def export_csv(content):
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    columns = (
        EXTENDED_COLUMNS
        if any("external_source" in movie for movie in content["movies"])
        else COLUMNS
    )
    writer.writerow(columns)
    for movie in content["movies"]:
        for entry in movie["copies"] or [None]:
            row = {
                "movie_id": movie["id"],
                "copy_id": entry["id"] if entry else "",
                **{k: movie[k] for k in ("title", "year", "director", "synopsis")},
                "actors": json.dumps(movie["actors"], ensure_ascii=False),
                "genres": json.dumps(movie["genres"], ensure_ascii=False),
                **{k: entry[k] if entry else "" for k in COPY_FIELDS},
            }
            if columns == EXTENDED_COLUMNS:
                row["external_source"] = (
                    json.dumps(movie["external_source"], ensure_ascii=False, separators=(",", ":"))
                    if "external_source" in movie
                    else ""
                )
            writer.writerow([csv_safe(row[k]) for k in columns])
    return "\ufeff" + output.getvalue()
