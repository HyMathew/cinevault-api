import uuid

from .enterprise_auth import now


def record_event(document, actor_id, entity_type, entity_id, action):
    document['audit_events'].append({'id':str(uuid.uuid4()),'actor_id':actor_id,
        'entity_type':entity_type,'entity_id':entity_id,'action':entity_type+'.'+action,'timestamp':now()})
    document['audit_events'][:] = document['audit_events'][-2000:]
