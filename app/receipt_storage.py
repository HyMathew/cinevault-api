"""Only explicitly allowed, non-authentication creation receipts are persisted."""
import hashlib
import json
import re

from .validation import SUPPORTS,is_unicode

HASH=re.compile(r'[0-9a-f]{64}')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def validate_receipts(document,ids,valid_uuid):
    records=document['idempotency_records']
    if len(records)>10000:
        raise ValueError('Receipt capacity exceeded')
    users={u['id'] for u in document['users']}
    keys=set()
    for record in records:
        if not isinstance(record,dict) or set(record)!={'id','actor_id','key_hash','request_hash','operation','created_at','expires_at','response'}:
            raise ValueError('Invalid receipt fields')
        if not valid_uuid(record['id']) or record['id'] in ids or record['actor_id'] not in users or record['operation']!='copy.create':
            raise ValueError('Invalid receipt identity')
        if any(not isinstance(record[k],str) or not HASH.fullmatch(record[k]) for k in ('key_hash','request_hash')):
            raise ValueError('Invalid receipt digest')
        if any(type(record[k]) is not int for k in ('created_at','expires_at')) or not 0<=record['created_at']<record['expires_at']<=record['created_at']+86400:
            raise ValueError('Invalid receipt timestamps')
        key=(record['actor_id'],record['key_hash'])
        if key in keys:
            raise ValueError('Duplicate receipt key')
        response=record['response']
        if not isinstance(response,dict) or set(response)!={'id','movie_id','support','location','condition','_etag'}:
            raise ValueError('Invalid cached response')
        if not valid_uuid(response['id']) or not valid_uuid(response['movie_id']) or response['support'] not in SUPPORTS or response['condition'] not in ('unknown','good','damaged'):
            raise ValueError('Invalid cached copy')
        if not isinstance(response['location'],str) or not is_unicode(response['location']) or len(response['location'])>200:
            raise ValueError('Invalid cached location')
        body={k:v for k,v in response.items() if k!='_etag'}
        request_body={k:v for k,v in body.items() if k!='id'}
        if response['_etag']!='"'+digest(body)+'"' or digest(request_body)!=record['request_hash']:
            raise ValueError('Inconsistent cached receipt')
        ids.add(record['id'])
        keys.add(key)
