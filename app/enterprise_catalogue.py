"""Catalogue resources; entity versions prevent silent stale replacements."""
import hashlib
import json
import uuid

from flask import Blueprint, jsonify, request, url_for

from .enterprise_auth import check_store, error, forbidden, no_cache, payload, protected
from .validation import ValidationError, is_unicode, validate

bp = Blueprint('enterprise_catalogue', __name__, url_prefix='/api/v1')
bp.after_request(no_cache)
MOVIE_FIELDS = ('title','year','director','actors','synopsis')
COPY_FIELDS = ('support','location','condition')
SUPPORTS = ('numerique','dvd','blu-ray','vhs','autre')


def etag(entity):
    encoded = json.dumps(entity, sort_keys=True, ensure_ascii=False, separators=(',',':')).encode('utf-8')
    return '"' + hashlib.sha256(encoded).hexdigest() + '"'


def resource(entity):
    return {**entity, '_etag':etag(entity)}


def respond(entity, status=200, location=None):
    response = jsonify(resource(entity))
    response.status_code = status
    response.headers['ETag'] = etag(entity)
    if location:
        response.headers['Location'] = location
    return response


def missing():
    return error(404, 'not_found', 'Ressource introuvable.')


def lookup(document, collection, identity):
    return next((item for item in document[collection] if item['id'] == identity), None)


def management(operation):
    def action(document, user, session):
        if user['role'] not in ('manager','admin'):
            return forbidden()
        return operation(document, user)
    return protected(action)


def precondition(entity):
    supplied = request.headers.get('If-Match')
    if not supplied:
        return error(428, 'version_required', 'La version de la ressource est requise.')
    if supplied != etag(entity):
        return error(412, 'stale_version', 'Cette ressource a changé. Rechargez-la avant de réessayer.')
    return None


def movie_data(data):
    legacy = validate({'titre':data['title'],'annee':data['year'],'realisateur':data['director'],
                       'acteurs':data['actors'],'synopsis':data['synopsis'],'support':'autre'})
    genres = data.get('genres', [])
    if not isinstance(genres, list) or len(genres) > 30 or any(not isinstance(g,str) or not is_unicode(g) or not 1 <= len(g) <= 80 or g.strip() != g for g in genres):
        raise ValidationError({'genres':'Liste de genres invalide.'})
    if len({g.casefold() for g in genres}) != len(genres):
        raise ValidationError({'genres':'Les genres doivent être distincts.'})
    return {'title':legacy['titre'],'year':legacy['annee'],'director':legacy['realisateur'],
            'actors':legacy['acteurs'],'synopsis':legacy['synopsis'],'genres':genres}


def copy_data(data):
    if not isinstance(data,dict) or set(data) != set(COPY_FIELDS):
        raise ValidationError({'copy':'Champs de l’exemplaire invalides.'})
    if data['support'] not in SUPPORTS or data['condition'] not in ('unknown','good','damaged') or not isinstance(data['location'],str) or not is_unicode(data['location']) or len(data['location']) > 200:
        raise ValidationError({'copy':'Attributs de l’exemplaire invalides.'})
    return dict(data)


def duplicate(document, candidate, excluded=None):
    key = (candidate['title'].casefold(),candidate['year'],candidate['director'].casefold())
    return any(m['id'] != excluded and (m['title'].casefold(),m['year'],m['director'].casefold()) == key for m in document['movies'])


def bounded_integer(name, default, lower, upper):
    try:
        value = int(request.args.get(name,default))
        if not lower <= value <= upper:
            raise ValueError()
        return value
    except ValueError:
        raise ValidationError({name:'Valeur invalide.'})


@bp.get('/movies')
def list_movies():
    allowed = {'q','genre','year','support','sort','limit','offset'}
    if set(request.args) - allowed or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise ValidationError({'query':'Paramètres invalides.'})
    limit, offset = bounded_integer('limit',24,1,100), bounded_integer('offset',0,0,10000)
    year = bounded_integer('year',1888,1888,2100) if 'year' in request.args else None
    query, genre = request.args.get('q','').strip().casefold(), request.args.get('genre','').strip().casefold()
    support, sort = request.args.get('support',''), request.args.get('sort','title')
    if len(query) > 200 or len(genre) > 80 or support not in ('', *SUPPORTS) or sort not in ('title','-title','year','-year'):
        raise ValidationError({'query':'Filtre ou tri invalide.'})
    document = check_store()
    support_ids = {c['movie_id'] for c in document['copies'] if c['support'] == support}
    movies = [m for m in document['movies'] if
              (not query or query in ' '.join([m['title'],m['director'],*m['actors']]).casefold()) and
              (not genre or genre in [g.casefold() for g in m['genres']]) and
              (year is None or m['year'] == year) and (not support or m['id'] in support_ids)]
    field = sort.lstrip('-')
    movies.sort(key=lambda m: (m[field].casefold() if field == 'title' else m[field], m['id']), reverse=sort.startswith('-'))
    page = movies[offset:offset+limit]
    summaries = {m['id']:{'count':0,'supports':[]} for m in page}
    for copy in document['copies']:
        if copy['movie_id'] in summaries:
            summary = summaries[copy['movie_id']]
            summary['count'] += 1
            if copy['support'] not in summary['supports']:
                summary['supports'].append(copy['support'])
    return {'movies':[resource(m) for m in page],'total':len(movies),'limit':limit,'offset':offset,'copy_summaries':summaries}


@bp.get('/movies/<movie_id>')
def get_movie(movie_id):
    movie = lookup(check_store(),'movies',movie_id)
    return missing() if movie is None else respond(movie)


@bp.post('/movies')
def create_movie():
    def create(document, user):
        data = payload(MOVIE_FIELDS, ('genres','initial_copy'))
        movie = movie_data(data)
        initial_copy = copy_data(data['initial_copy']) if 'initial_copy' in data else None
        if duplicate(document,movie):
            return error(409,'duplicate','Ce film existe déjà.')
        movie['id'] = str(uuid.uuid4())
        document['movies'].append(movie)
        if initial_copy is not None:
            document['copies'].append({'id':str(uuid.uuid4()),'movie_id':movie['id'],**initial_copy})
        return respond(movie,201,url_for('enterprise_catalogue.get_movie',movie_id=movie['id']))
    return management(create)


@bp.put('/movies/<movie_id>')
def replace_movie(movie_id):
    def replace(document, user):
        movie = lookup(document,'movies',movie_id)
        if movie is None:
            return missing()
        conflict = precondition(movie)
        if conflict:
            return conflict
        data = movie_data(payload(MOVIE_FIELDS, ('genres',)))
        if duplicate(document,data,movie_id):
            return error(409,'duplicate','Ce film existe déjà.')
        movie.update(data)
        return respond(movie)
    return management(replace)


@bp.delete('/movies/<movie_id>')
def delete_movie(movie_id):
    def delete(document,user):
        movie = lookup(document,'movies',movie_id)
        if movie is None:
            return missing()
        conflict = precondition(movie)
        if conflict:
            return conflict
        if any(c['movie_id'] == movie_id for c in document['copies']):
            return error(409,'referenced','Traitez les exemplaires avant de supprimer le film.')
        document['movies'].remove(movie)
        return '',204
    return management(delete)


@bp.get('/copies')
def list_copies():
    if set(request.args) - {'movie_id'} or len(request.args.getlist('movie_id')) > 1:
        raise ValidationError({'query':'Paramètres invalides.'})
    movie_id = request.args.get('movie_id')
    return {'copies':[resource(c) for c in check_store()['copies'] if movie_id is None or c['movie_id'] == movie_id]}


@bp.get('/copies/<copy_id>')
def get_copy(copy_id):
    copy = lookup(check_store(),'copies',copy_id)
    return missing() if copy is None else respond(copy)


@bp.post('/copies')
def create_copy():
    def create(document,user):
        data = payload(('movie_id',*COPY_FIELDS))
        attributes = copy_data({k:data[k] for k in COPY_FIELDS})
        if lookup(document,'movies',data['movie_id']) is None:
            return missing()
        copy = {'id':str(uuid.uuid4()),'movie_id':data['movie_id'],**attributes}
        document['copies'].append(copy)
        return respond(copy,201,url_for('enterprise_catalogue.get_copy',copy_id=copy['id']))
    return management(create)


@bp.put('/copies/<copy_id>')
def replace_copy(copy_id):
    def replace(document,user):
        copy = lookup(document,'copies',copy_id)
        if copy is None:
            return missing()
        conflict = precondition(copy)
        if conflict:
            return conflict
        copy.update(copy_data(payload(COPY_FIELDS)))
        return respond(copy)
    return management(replace)


@bp.delete('/copies/<copy_id>')
def delete_copy(copy_id):
    def delete(document,user):
        copy = lookup(document,'copies',copy_id)
        if copy is None:
            return missing()
        conflict = precondition(copy)
        if conflict:
            return conflict
        document['copies'].remove(copy)
        return '',204
    return management(delete)
