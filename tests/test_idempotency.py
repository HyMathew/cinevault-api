from test_enterprise_auth import enterprise,account
from test_enterprise_catalogue import MOVIE,COPY

KEY='synthetic-operation-key-123456789'


def setup(enterprise):
    client,path=enterprise
    _,auth=account(client,'receiptmanager','manager',path)
    movie=client.post('/api/v1/movies',headers=auth,json=MOVIE).json
    return client,path,movie,auth


def test_repeated_copy_creation_returns_original_receipt_without_duplicate(enterprise):
    client,path,movie,auth=setup(enterprise)
    headers={**auth,'Idempotency-Key':KEY}
    body={'movie_id':movie['id'],**COPY}
    first=client.post('/api/v1/copies',headers=headers,json=body)
    second=client.post('/api/v1/copies',headers=headers,json=body)
    assert first.status_code==second.status_code==201
    assert first.json==second.json
    assert first.headers['Location']==second.headers['Location']
    assert len(client.get('/api/v1/copies').json['copies'])==1
    assert KEY.encode() not in path.read_bytes()
    changed=client.post('/api/v1/copies',headers=headers,json={**body,'location':'different'})
    assert changed.status_code==409
    assert len(client.get('/api/v1/copies').json['copies'])==1


def test_receipts_are_scoped_to_actor_and_permissions_are_rechecked(enterprise):
    client,path,movie,auth=setup(enterprise)
    _,other=account(client,'otherreceipt','manager',path)
    _,member=account(client,'memberreceipt')
    body={'movie_id':movie['id'],**COPY}
    first=client.post('/api/v1/copies',headers={**auth,'Idempotency-Key':KEY},json=body)
    second=client.post('/api/v1/copies',headers={**other,'Idempotency-Key':KEY},json=body)
    assert first.json['id']!=second.json['id']
    assert client.post('/api/v1/copies',headers={**member,'Idempotency-Key':KEY},json=body).status_code==403
    for key in ('','short','bad key with spaces'):
        assert client.post('/api/v1/copies',headers={**auth,'Idempotency-Key':key},json=body).status_code==422
    from app.storage_v2 import V2Store
    V2Store(path).transact(lambda doc: next(u for u in doc['users'] if u['username']=='receiptmanager').update(role='member'))
    assert client.post('/api/v1/copies',headers={**auth,'Idempotency-Key':KEY},json=body).status_code==403


def test_receipt_remains_valid_after_created_resource_is_removed(enterprise):
    client,path,movie,auth=setup(enterprise)
    headers={**auth,'Idempotency-Key':KEY}
    body={'movie_id':movie['id'],**COPY}
    first=client.post('/api/v1/copies',headers=headers,json=body)
    assert client.delete('/api/v1/copies/'+first.json['id'],headers={**auth,'If-Match':first.json['_etag']}).status_code==204
    assert client.delete('/api/v1/movies/'+movie['id'],headers={**auth,'If-Match':movie['_etag']}).status_code==204
    replay=client.post('/api/v1/copies',headers=headers,json=body)
    assert replay.status_code==201 and replay.json==first.json
    assert client.get(replay.headers['Location']).status_code==404


def test_failed_creation_does_not_cache_or_corrupt_store(enterprise):
    from app.storage_v2 import V2Store
    client,path,movie,auth=setup(enterprise)
    response=client.post('/api/v1/copies',headers={**auth,'Idempotency-Key':KEY},json={'movie_id':'missing',**COPY})
    assert response.status_code==404
    assert V2Store(path).read()['idempotency_records']==[]
    response=client.post('/api/v1/copies',headers={**auth,'Idempotency-Key':KEY},json={'movie_id':'\ud800',**COPY})
    assert response.status_code==422


def create_in_process(path,auth,body,queue):
    from pathlib import Path
    from app import create_app
    app=create_app({'TESTING':True,'DATA_FILE':str(Path(path).parent/'legacy.json'),
        'ENTERPRISE_ENABLED':True,'ENTERPRISE_DATA_FILE':str(path),'AUTH_THROTTLE_KEY':b'x'*32})
    response=app.test_client().post('/api/v1/copies',headers={**auth,'Idempotency-Key':KEY},json=body)
    queue.put((response.status_code,response.json['id']))


def test_two_processes_replay_one_creation_and_receipt_matches_schema(enterprise):
    import json,multiprocessing
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from app.storage_v2 import V2Store
    client,path,movie,auth=setup(enterprise)
    context=multiprocessing.get_context('spawn')
    queue=context.Queue()
    processes=[context.Process(target=create_in_process,args=(path,auth,{'movie_id':movie['id'],**COPY},queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(25)
        if process.is_alive():
            process.terminate(); process.join()
            raise AssertionError('Receipt process timed out')
        assert process.exitcode==0
    results=[queue.get(timeout=5) for _ in processes]
    assert all(status==201 for status,_ in results) and results[0][1]==results[1][1]
    document=V2Store(path).read()
    assert len(document['copies'])==len(document['idempotency_records'])==1
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/catalogue-v2.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator(schema).validate(document)


def test_receipt_expiration_allows_a_new_operation_after_24_hours(enterprise):
    from app.storage_v2 import V2Store
    client,path,movie,auth=setup(enterprise)
    headers={**auth,'Idempotency-Key':KEY}
    body={'movie_id':movie['id'],**COPY}
    first=client.post('/api/v1/copies',headers=headers,json=body)
    record=V2Store(path).read()['idempotency_records'][0]
    client.application.config['AUTH_CLOCK']=lambda:record['expires_at']+1
    login=client.post('/api/v1/auth/login',json={'username':'receiptmanager','password':'test passphrase 123'})
    assert login.status_code==200
    again=client.post('/api/v1/copies',headers={'Authorization':'Bearer '+login.json['token'],'Idempotency-Key':KEY},json=body)
    assert again.status_code==201 and again.json['id']!=first.json['id']
    assert len(V2Store(path).read()['idempotency_records'])==1
