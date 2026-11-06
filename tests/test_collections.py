from test_enterprise_auth import enterprise, account
from test_enterprise_catalogue import MOVIE


def context(enterprise):
    client,path = enterprise
    _,admin = account(client,'admincollections','admin',path)
    movie = client.post('/api/v1/movies',headers=admin,json=MOVIE).json
    alice,auth = account(client,'alicecollections')
    _,other = account(client,'othercollections')
    return client,path,movie,auth,other,admin


def test_private_collection_ownership_and_duplicate_protection(enterprise):
    client,path,movie,auth,other,admin = context(enterprise)
    assert client.get('/api/v1/collections').status_code == 401
    entry = client.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'favorite'})
    assert entry.status_code == 201
    endpoint = '/api/v1/collections/'+entry.json['id']
    for header in (other,admin):
        assert client.get('/api/v1/collections',headers=header).json['total'] == 0
        assert client.get(endpoint,headers=header).status_code == 404
        assert client.delete(endpoint,headers={**header,'If-Match':entry.json['_etag']}).status_code == 404
    assert client.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'favorite'}).status_code == 409
    assert client.delete('/api/v1/movies/'+movie['id'],headers={**admin,'If-Match':movie['_etag']}).status_code == 409
    assert client.delete(endpoint,headers=auth).status_code == 428
    assert client.delete(endpoint,headers={**auth,'If-Match':entry.json['_etag']}).status_code == 204
    assert client.delete('/api/v1/movies/'+movie['id'],headers={**admin,'If-Match':movie['_etag']}).status_code == 204


def test_custom_lists_owned_supports_and_stale_versions(enterprise):
    client,path,movie,auth,other,admin = context(enterprise)
    response = client.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'custom','list_name':'Mes classiques'})
    assert response.status_code == 201
    entry = response.json
    endpoint = '/api/v1/collections/'+entry['id']
    updated = client.put(endpoint,headers={**auth,'If-Match':entry['_etag']},json={'list_name':'À partager','supports':[]})
    assert updated.status_code == 200
    assert client.delete(endpoint,headers={**auth,'If-Match':entry['_etag']}).status_code == 412
    owned = client.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'owned','supports':['dvd','blu-ray']})
    assert owned.status_code == 201
    listing = client.get('/api/v1/collections',headers=auth,query_string={'kind':'owned'}).json
    assert listing['total'] == 1 and listing['entries'][0]['supports'] == ['dvd','blu-ray']
    for invalid in ({'owner_id':'other'},{'kind':'admin'},{'kind':'custom'},{'supports':['bad']},{'supports':['dvd','dvd']}):
        assert client.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'owned',**invalid}).status_code == 422


def test_audit_minimizes_data_and_is_admin_only(enterprise):
    client,path,movie,auth,other,admin = context(enterprise)
    client.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'custom','list_name':'private collection name'})
    assert client.get('/api/v1/audit-events',headers=auth).status_code == 403
    response = client.get('/api/v1/audit-events',headers=admin)
    assert response.status_code == 200
    assert b'private collection name' not in response.data
    assert any(event['action'] == 'collection.created' for event in response.json['events'])
    assert all(set(event) == {'id','actor_id','action','entity_type','entity_id','timestamp'} for event in response.json['events'])


def test_collection_document_and_response_schemas(enterprise):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from app.storage_v2 import V2Store
    client,path,movie,auth,other,admin = context(enterprise)
    response = client.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'owned','supports':['dvd']})
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root/'schemas/catalogue-v2.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(V2Store(path).read())
    spec = json.loads((root/'docs/openapi-v1.json').read_text(encoding='utf-8'))
    Draft202012Validator({'$ref':'#/components/schemas/CollectionEntry','components':spec['components']}).validate(response.json)
    Draft202012Validator({'$ref':'#/components/schemas/CollectionList','components':spec['components']}).validate(client.get('/api/v1/collections',headers=auth).json)


def test_concurrent_duplicate_entry_creates_one_record_and_one_event(enterprise):
    from concurrent.futures import ThreadPoolExecutor
    from app.storage_v2 import V2Store
    client,path,movie,auth,other,admin = context(enterprise)
    def create(_):
        with client.application.test_client() as separate:
            return separate.post('/api/v1/collections',headers=auth,json={'movie_id':movie['id'],'kind':'favorite'}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(create,range(2))) == [201,409]
    document = V2Store(path).read()
    assert len(document['collections']) == 1
    assert len([e for e in document['audit_events'] if e['action'] == 'collection.created']) == 1


def test_all_personal_collection_kinds_have_distinct_entries(enterprise):
    client,path,movie,auth,other,admin = context(enterprise)
    for kind in ('owned','favorite','watched','watchlist','custom'):
        body = {'movie_id':movie['id'],'kind':kind}
        if kind == 'custom':
            body['list_name'] = 'Mes soirées'
        assert client.post('/api/v1/collections',headers=auth,json=body).status_code == 201
        response = client.get('/api/v1/collections',headers=auth,query_string={'kind':kind})
        assert response.json['total'] == 1 and response.json['entries'][0]['kind'] == kind
