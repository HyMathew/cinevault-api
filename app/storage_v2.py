"""Versioned single-document JSON storage; independent of the legacy HTTP API."""
import json
import hashlib
import hmac
import os
import tempfile
import uuid
from pathlib import Path

import portalocker

from .repository import unique_pairs
from .validation import ValidationError, is_unicode, validate
from .auth_storage import validate_auth
from .business_storage import validate_business

COLLECTIONS = ('users', 'sessions', 'auth_attempts', 'idempotency_records', 'movies',
               'copies', 'collections', 'loans', 'reservations', 'audit_events')


def empty_document():
    return {'schema_version': 2, 'revision': 0, **{name: [] for name in COLLECTIONS}}


def _valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_document(data):
    """Validate the implemented v2 subset; unimplemented entities fail closed."""
    if not isinstance(data, dict) or set(data) != {'schema_version', 'revision', *COLLECTIONS}:
        raise ValueError('Invalid document fields')
    if type(data['schema_version']) is not int or data['schema_version'] != 2 or type(data['revision']) is not int or data['revision'] < 0:
        raise ValueError('Invalid schema version or revision')
    for name in COLLECTIONS:
        if not isinstance(data[name], list):
            raise ValueError('Invalid collection')
        if name not in ('movies', 'copies', 'users', 'sessions', 'auth_attempts', 'collections', 'audit_events') and data[name]:
            raise ValueError('Entity not yet supported')
    ids = set()
    movie_ids = set()
    for movie in data['movies']:
        if not isinstance(movie, dict) or set(movie) != {'id', 'title', 'year', 'director', 'actors', 'synopsis', 'genres'}:
            raise ValueError('Invalid movie fields')
        if not _valid_uuid(movie['id']) or movie['id'] in ids:
            raise ValueError('Invalid or duplicate id')
        ids.add(movie['id'])
        movie_ids.add(movie['id'])
        legacy = {'titre': movie['title'], 'annee': movie['year'], 'realisateur': movie['director'], 'acteurs': movie['actors'], 'synopsis': movie['synopsis'], 'support': 'autre'}
        try:
            if validate(legacy) != legacy:
                raise ValueError('Non-canonical movie')
        except ValidationError as exc:
            raise ValueError('Invalid movie') from exc
        genres = movie['genres']
        if not isinstance(genres, list) or len(genres) > 30 or any(not isinstance(g, str) or not is_unicode(g) or not 1 <= len(g) <= 80 or g.strip() != g for g in genres) or len(set(genres)) != len(genres):
            raise ValueError('Invalid genres')
    for copy in data['copies']:
        if not isinstance(copy, dict) or set(copy) != {'id', 'movie_id', 'support', 'location', 'condition'}:
            raise ValueError('Invalid copy fields')
        if not _valid_uuid(copy['id']) or copy['id'] in ids or not _valid_uuid(copy['movie_id']) or copy['movie_id'] not in movie_ids:
            raise ValueError('Invalid copy id or reference')
        ids.add(copy['id'])
        if copy['support'] not in ('numerique', 'dvd', 'blu-ray', 'vhs', 'autre') or copy['condition'] not in ('unknown', 'good', 'damaged'):
            raise ValueError('Invalid copy enum')
        if not isinstance(copy['location'], str) or not is_unicode(copy['location']) or len(copy['location']) > 200:
            raise ValueError('Invalid location')
    validate_auth(data, ids, _valid_uuid)
    validate_business(data, ids, _valid_uuid)
    return data


def decode_document(data):
    try:
        return validate_document(json.loads(data, object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid constant'))))
    except (UnicodeDecodeError, RecursionError, TypeError) as exc:
        raise ValueError('Invalid document') from exc


def _encode(document):
    return (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _sync_directory(directory):
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_new(path, data):
    """Create atomically without replacing an existing destination."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.cinevault-v2-', delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _replace(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.cinevault-v2-', delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _sync_directory(path.parent)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class V2Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = str(self.path) + '.lock'

    def initialize(self):
        with portalocker.Lock(self.lock_path, mode='a', timeout=10):
            _write_new(self.path, _encode(empty_document()))

    def read(self):
        with portalocker.Lock(self.lock_path, mode='a', timeout=10):
            return decode_document(self.path.read_bytes())

    def transact(self, mutation):
        with portalocker.Lock(self.lock_path, mode='a', timeout=10):
            document = decode_document(self.path.read_bytes())
            revision = document['revision']
            result = mutation(document)
            if document.get('revision') != revision:
                raise ValueError('Revision is managed by store')
            document['revision'] = revision + 1
            validate_document(document)
            _replace(self.path, _encode(document))
            return result

    def snapshot(self, destination):
        with portalocker.Lock(self.lock_path, mode='a', timeout=10):
            data = self.path.read_bytes()
            decode_document(data)
            _write_new(destination, data)
            return hashlib.sha256(data).hexdigest()

    def restore(self, snapshot, expected_sha256):
        with portalocker.Lock(self.lock_path, mode='a', timeout=10):
            data = Path(snapshot).read_bytes()
            if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256):
                raise ValueError('Snapshot digest mismatch')
            decode_document(data)
            _write_new(self.path, data)
