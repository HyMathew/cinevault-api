"""Cross-reference validation for the implemented private collection subset."""
from .validation import SUPPORTS, is_unicode

KINDS = ('owned','favorite','watched','watchlist','custom')
AUDIT_TYPES = ('movie','copy','collection')
AUDIT_ACTIONS = tuple(f'{kind}.{action}' for kind in AUDIT_TYPES for action in ('created','updated','deleted'))


def valid_collection_attributes(kind, name, supports):
    if kind not in KINDS or not isinstance(name,str) or not is_unicode(name):
        return False
    if (kind == 'custom' and (not 1 <= len(name) <= 80 or name.strip() != name or any(ord(c) < 32 for c in name))) or (kind != 'custom' and name != ''):
        return False
    if not isinstance(supports,list) or len(supports) > 5 or any(not isinstance(s,str) or s not in SUPPORTS for s in supports) or len(set(supports)) != len(supports):
        return False
    return kind == 'owned' or supports == []


def validate_business(document, ids, valid_uuid):
    if len(document['collections']) > 10000 or len(document['audit_events']) > 2000:
        raise ValueError('Business collection limit exceeded')
    users = {u['id'] for u in document['users']}
    movies = {m['id'] for m in document['movies']}
    keys = set()
    for entry in document['collections']:
        if not isinstance(entry,dict) or set(entry) != {'id','owner_id','movie_id','kind','list_name','supports','created_at'}:
            raise ValueError('Invalid collection entry fields')
        if not valid_uuid(entry['id']) or entry['id'] in ids or entry['owner_id'] not in users or entry['movie_id'] not in movies:
            raise ValueError('Invalid collection reference')
        if not valid_collection_attributes(entry['kind'],entry['list_name'],entry['supports']) or type(entry['created_at']) is not int or entry['created_at'] < 0:
            raise ValueError('Invalid collection attributes')
        key = (entry['owner_id'],entry['movie_id'],entry['kind'],entry['list_name'].casefold())
        if key in keys:
            raise ValueError('Duplicate collection entry')
        ids.add(entry['id'])
        keys.add(key)
    for event in document['audit_events']:
        if not isinstance(event,dict) or set(event) != {'id','actor_id','action','entity_type','entity_id','timestamp'}:
            raise ValueError('Invalid audit event fields')
        if not valid_uuid(event['id']) or event['id'] in ids or event['actor_id'] not in users or not valid_uuid(event['entity_id']):
            raise ValueError('Invalid audit reference')
        if event['entity_type'] not in AUDIT_TYPES or event['action'] not in AUDIT_ACTIONS or not event['action'].startswith(event['entity_type']+'.') or type(event['timestamp']) is not int or event['timestamp'] < 0:
            raise ValueError('Invalid audit event')
        ids.add(event['id'])
