"""Canonical catalogue query values and Unicode keys shared by both adapters."""

from dataclasses import dataclass

from .validation import SUPPORTS, ValidationError, is_unicode

MOVIE_KEYS = frozenset({"search_key", "title_key", "director_key", "genre_keys"})


def movie_keys(movie):
    if (
        not isinstance(movie.get("title"), str)
        or not isinstance(movie.get("director"), str)
        or type(movie.get("actors")) is not list
        or type(movie.get("genres")) is not list
        or any(not isinstance(value, str) for value in [*movie["actors"], *movie["genres"]])
    ):
        raise ValueError("Movie keys require canonical text fields")
    return {
        "title_key": movie["title"].casefold(),
        "director_key": movie["director"].casefold(),
        "search_key": " ".join([movie["title"], movie["director"], *movie["actors"]]).casefold(),
        "genre_keys": [genre.casefold() for genre in movie["genres"]],
    }


@dataclass(frozen=True)
class MovieQuery:
    query: str = ""
    genre: str = ""
    year: int | None = None
    support: str = ""
    sort: str = "title"
    limit: int = 24
    offset: int = 0

    def __post_init__(self):
        if (
            any(
                type(value) is not str or not is_unicode(value)
                for value in (self.query, self.genre, self.support, self.sort)
            )
            or self.query != self.query.strip().casefold()
            or self.genre != self.genre.strip().casefold()
            or len(self.query) > 200
            or len(self.genre) > 80
            or self.support not in ("", *SUPPORTS)
            or self.sort not in ("title", "-title", "year", "-year")
            or type(self.limit) is not int
            or not 1 <= self.limit <= 100
            or type(self.offset) is not int
            or not 0 <= self.offset <= 10000
            or (
                self.year is not None
                and (type(self.year) is not int or not 1888 <= self.year <= 2100)
            )
        ):
            raise ValueError("Invalid canonical movie query")


def query_values(parameters, allowed):
    values = {}
    for name, value in parameters:
        if name not in allowed or name in values or type(value) is not str or not is_unicode(value):
            raise ValidationError({"query": "Paramètres invalides."})
        values[name] = value
    return values


def parse_movie_query(parameters):
    values = query_values(parameters, {"q", "genre", "year", "support", "sort", "limit", "offset"})

    def integer(name, default, lower, upper):
        try:
            value = int(values.get(name, default))
            if not lower <= value <= upper:
                raise ValueError()
            return value
        except ValueError:
            raise ValidationError({name: "Valeur invalide."}) from None

    limit, offset = integer("limit", 24, 1, 100), integer("offset", 0, 0, 10000)
    year = integer("year", 1888, 1888, 2100) if "year" in values else None
    try:
        return MovieQuery(
            query=values.get("q", "").strip().casefold(),
            genre=values.get("genre", "").strip().casefold(),
            year=year,
            support=values.get("support", ""),
            sort=values.get("sort", "title"),
            limit=limit,
            offset=offset,
        )
    except ValueError:
        raise ValidationError({"query": "Filtre ou tri invalide."}) from None
