import json
import os
import tempfile
import uuid
from pathlib import Path
import portalocker
from .validation import ValidationError, validate

class StorageError(Exception):
    pass

class NotFound(Exception):
    pass

def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key')
        result[key] = value
    return result

class Repository:
    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = str(self.path) + '.lock'

    def _read(self):
        if not self.path.exists():
            return []
        with self.path.open(encoding='utf-8') as stream:
            data = json.load(stream, object_pairs_hook=unique_pairs)
        if not isinstance(data, dict) or set(data) != {'films'} or not isinstance(data['films'], list):
            raise ValueError('Invalid root')
        ids = set()
        for item in data['films']:
            if not isinstance(item, dict) or set(item) != {'id', 'titre', 'annee', 'realisateur', 'acteurs', 'support', 'synopsis'}:
                raise ValueError('Invalid item')
            if not isinstance(item['id'], str) or str(uuid.UUID(item['id'])) != item['id'] or item['id'] in ids:
                raise ValueError('Invalid id')
            ids.add(item['id'])
            clean = validate({k: v for k, v in item.items() if k != 'id'})
            if clean != {k: v for k, v in item.items() if k != 'id'}:
                raise ValueError('Non-canonical data')
        return data['films']

    def _write(self, films):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, prefix='.films-', suffix='.tmp', delete=False) as stream:
                temporary = stream.name
                json.dump({'films': films}, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            if os.name != 'nt':
                fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def execute(self, operation):
        try:
            with portalocker.Lock(self.lock_path, mode='a', timeout=10):
                films = self._read()
                result, changed = operation(films)
                if changed or not self.path.exists():
                    self._write(films)
                return result
        except NotFound:
            raise
        except (OSError, ValueError, TypeError, RecursionError, ValidationError, portalocker.exceptions.LockException) as exc:
            raise StorageError('Stockage inaccessible ou invalide') from exc
