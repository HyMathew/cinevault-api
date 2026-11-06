import json
import os
import uuid
from flask import Flask, jsonify, request, url_for
from werkzeug.exceptions import HTTPException, UnsupportedMediaType, BadRequest
from .repository import NotFound, Repository, StorageError, unique_pairs
from .validation import ValidationError, validate

def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DATA_FILE=os.environ.get('DATA_FILE', '/data/films.json'), MAX_CONTENT_LENGTH=65536,
                      ENTERPRISE_ENABLED=os.environ.get('ENTERPRISE_ENABLED', '').lower() == 'true',
                      ENTERPRISE_DATA_FILE=os.environ.get('ENTERPRISE_DATA_FILE'),
                      AUTH_THROTTLE_KEY_FILE=os.environ.get('AUTH_THROTTLE_KEY_FILE'))
    if config:
        app.config.update(config)
    repo = Repository(app.config['DATA_FILE'])

    def error(status, code, message, fields=None):
        body = {'code': code, 'message': message}
        if fields is not None:
            body['fields'] = fields
        return jsonify(error=body), status

    if app.config['ENTERPRISE_ENABLED'] is True:
        from .enterprise_auth import install_enterprise
        install_enterprise(app)

        @app.before_request
        def block_legacy_mutation():
            if (request.path == '/api/films' or request.path.startswith('/api/films/')) and request.method not in ('GET', 'HEAD', 'OPTIONS'):
                return error(403, 'legacy_write_disabled', 'Écriture legacy désactivée en mode Enterprise.')

    @app.errorhandler(StorageError)
    def storage_error(exc):
        app.logger.error('Stockage inaccessible ou invalide: %s', type(exc.__cause__).__name__)
        return error(503, 'storage_unavailable', 'Stockage indisponible.')

    @app.errorhandler(ValidationError)
    def validation_error(exc):
        return error(422, 'validation_error', 'Données invalides.', exc.fields)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        body, _ = error(exc.code, exc.name.lower().replace(' ', '_'), exc.description)
        response = exc.get_response()
        response.set_data(body.get_data())
        response.content_type = 'application/json'
        return response

    @app.errorhandler(Exception)
    def internal_error(exc):
        app.logger.exception('Erreur interne')
        return error(500, 'internal_error', 'Erreur interne.')

    def payload():
        if not request.is_json:
            raise UnsupportedMediaType()
        try:
            data = json.loads(request.get_data(), object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise BadRequest('JSON invalide.')
        return validate(data)

    def lookup(films, film_id):
        for film in films:
            if film['id'] == film_id:
                return film
        raise NotFound()

    @app.errorhandler(NotFound)
    def not_found(exc):
        return error(404, 'not_found', 'Film introuvable.')

    @app.get('/health')
    def health():
        if app.config['ENTERPRISE_ENABLED'] is True:
            from .enterprise_auth import check_store
            check_store()
        else:
            repo.execute(lambda films: (None, False))
        return {'status': 'ok'}

    @app.get('/api/films')
    def list_films():
        query = request.args.get('q', '').strip().casefold()
        support = request.args.get('support', '').strip()
        films = repo.execute(lambda items: (list(items), False))
        films = [f for f in films if (not query or query in f['titre'].casefold() or query in f['realisateur'].casefold()) and (not support or f['support'] == support)]
        return {'films': sorted(films, key=lambda f: (f['titre'].casefold(), f['id']))}

    @app.get('/api/films/<film_id>')
    def get_film(film_id):
        return repo.execute(lambda films: (lookup(films, film_id), False))

    @app.post('/api/films')
    def create_film():
        film = {'id': str(uuid.uuid4()), **payload()}
        def create(films):
            films.append(film)
            return film, True
        result = repo.execute(create)
        return jsonify(result), 201, {'Location': url_for('get_film', film_id=film['id'])}

    @app.put('/api/films/<film_id>')
    def replace_film(film_id):
        data = payload()
        def replace(films):
            film = lookup(films, film_id)
            film.clear()
            film.update({'id': film_id, **data})
            return film, True
        return repo.execute(replace)

    @app.delete('/api/films/<film_id>')
    def delete_film(film_id):
        def delete(films):
            films.remove(lookup(films, film_id))
            return None, True
        repo.execute(delete)
        return '', 204

    return app
