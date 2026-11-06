import uuid

from flask import Blueprint, request, url_for

from .audit import record_event
from .business_storage import KINDS, valid_collection_attributes
from .enterprise_auth import error, forbidden, no_cache, now, payload, protected
from .enterprise_catalogue import bounded_integer, lookup, missing, precondition, resource, respond
from .validation import ValidationError

bp = Blueprint('enterprise_collections',__name__,url_prefix='/api/v1')
bp.after_request(no_cache)


def own(document,user,identity):
    item = lookup(document,'collections',identity)
    return item if item and item['owner_id'] == user['id'] else None


def duplicate(document,candidate,excluded=None):
    return any(e['id'] != excluded and all(e[k] == candidate[k] for k in ('owner_id','movie_id','kind')) and
               e['list_name'].casefold() == candidate['list_name'].casefold() for e in document['collections'])


def attributes(kind,data):
    name,supports = data.get('list_name',''),data.get('supports',[])
    if not valid_collection_attributes(kind,name,supports):
        raise ValidationError({'body':'Type, nom de liste ou supports invalides.'})
    return {'list_name':name,'supports':supports}


@bp.get('/collections')
def list_entries():
    def listing(document,user,session):
        if set(request.args) - {'kind','list_name','limit','offset'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
            raise ValidationError({'query':'Paramètres invalides.'})
        kind,name = request.args.get('kind',''),request.args.get('list_name','')
        if kind not in ('',*KINDS) or len(name) > 80:
            raise ValidationError({'query':'Filtre invalide.'})
        limit,offset = bounded_integer('limit',24,1,100),bounded_integer('offset',0,0,10000)
        entries = [e for e in document['collections'] if e['owner_id'] == user['id'] and
                   (not kind or e['kind'] == kind) and (not name or e['list_name'].casefold() == name.casefold())]
        entries.sort(key=lambda e:(e['created_at'],e['id']),reverse=True)
        page = entries[offset:offset+limit]
        movie_ids = {e['movie_id'] for e in page}
        return {'entries':[resource(e) for e in page],'total':len(entries),'limit':limit,'offset':offset,
                'movies':{m['id']:resource(m) for m in document['movies'] if m['id'] in movie_ids}}
    return protected(listing)


@bp.post('/collections')
def create_entry():
    def create(document,user,session):
        data = payload(('movie_id','kind'),('list_name','supports'))
        if not isinstance(data['movie_id'],str):
            raise ValidationError({'movie_id':'Identifiant de film invalide.'})
        attrs = attributes(data['kind'],data)
        if lookup(document,'movies',data['movie_id']) is None:
            return missing()
        entry = {'id':str(uuid.uuid4()),'owner_id':user['id'],'movie_id':data['movie_id'],
                 'kind':data['kind'],**attrs,'created_at':now()}
        if duplicate(document,entry):
            return error(409,'duplicate','Cette entrée existe déjà dans votre collection.')
        document['collections'].append(entry)
        record_event(document,user['id'],'collection',entry['id'],'created')
        return respond(entry,201,url_for('enterprise_collections.get_entry',entry_id=entry['id']))
    return protected(create)


@bp.get('/collections/<entry_id>')
def get_entry(entry_id):
    def get(document,user,session):
        entry = own(document,user,entry_id)
        return missing() if entry is None else respond(entry)
    return protected(get)


@bp.put('/collections/<entry_id>')
def replace_entry(entry_id):
    def replace(document,user,session):
        entry = own(document,user,entry_id)
        if entry is None:
            return missing()
        conflict = precondition(entry)
        if conflict:
            return conflict
        data = attributes(entry['kind'],payload(('list_name','supports')))
        if duplicate(document,{**entry,**data},entry_id):
            return error(409,'duplicate','Cette entrée existe déjà dans votre collection.')
        entry.update(data)
        record_event(document,user['id'],'collection',entry_id,'updated')
        return respond(entry)
    return protected(replace)


@bp.delete('/collections/<entry_id>')
def delete_entry(entry_id):
    def delete(document,user,session):
        entry = own(document,user,entry_id)
        if entry is None:
            return missing()
        conflict = precondition(entry)
        if conflict:
            return conflict
        document['collections'].remove(entry)
        record_event(document,user['id'],'collection',entry_id,'deleted')
        return '',204
    return protected(delete)


@bp.get('/audit-events')
def list_events():
    def listing(document,user,session):
        if user['role'] != 'admin':
            return forbidden()
        if set(request.args) - {'limit','offset'}:
            raise ValidationError({'query':'Paramètres invalides.'})
        limit,offset = bounded_integer('limit',50,1,100),bounded_integer('offset',0,0,10000)
        events = list(reversed(document['audit_events']))
        return {'events':events[offset:offset+limit],'total':len(events),'limit':limit,'offset':offset}
    return protected(listing)
