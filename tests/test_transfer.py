from test_enterprise_auth import enterprise,account
from test_enterprise_catalogue import MOVIE,COPY

KEY='synthetic-import-key-123456789'


def context(enterprise):
    client,path=enterprise
    _,manager=account(client,'transfermanager','manager',path)
    _,member=account(client,'transfermember')
    return client,path,manager,member


def batch(title='Imported film'):
    return {'format':'json','content':{'format_version':1,'movies':[{**MOVIE,'title':title,'copies':[COPY]}]},'duplicate_policy':'reject'}


def test_import_export_permissions_and_no_private_data(enterprise):
    client,path,manager,member=context(enterprise)
    route='/api/v1/catalogue/import'
    assert client.post(route,json=batch()).status_code==401
    assert client.post(route,headers=member,json=batch()).status_code==403
    assert client.post(route,headers=manager,json=batch()).status_code==428
    headers={**manager,'Idempotency-Key':KEY}
    first=client.post(route,headers=headers,json=batch())
    assert first.status_code==200
    assert first.json['created_movies']==first.json['created_copies']==1
    again=client.post(route,headers=headers,json=batch())
    assert again.status_code==200 and again.json==first.json
    assert client.post(route,headers=headers,json=batch('different')).status_code==409
    exported=client.get('/api/v1/catalogue/export?format=json',headers=manager)
    assert exported.status_code==200
    assert set(exported.json['content'])=={'format_version','movies'}
    assert len(exported.json['content']['movies'])==1
    assert 'password' not in exported.text and 'transfermember' not in exported.text
    assert client.get('/api/v1/catalogue/export',headers=member).status_code==403


def test_invalid_import_is_atomic_and_audited(enterprise):
    from app.storage_v2 import V2Store
    client,path,manager,member=context(enterprise)
    data=batch()
    data['content']['movies'].append({**MOVIE,'title':'Bad','year':True,'copies':[COPY]})
    response=client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY},json=data)
    assert response.status_code==422
    document=V2Store(path).read()
    assert document['movies']==document['copies']==[]
    assert document['idempotency_records']==[]
    assert document['audit_events'][-1]['action']=='import.failed'


def test_duplicate_policies_preserve_existing_metadata(enterprise):
    client,path,manager,member=context(enterprise)
    route='/api/v1/catalogue/import'
    assert client.post(route,headers={**manager,'Idempotency-Key':KEY},json=batch()).status_code==200
    for policy,status in [('reject',409),('skip',200),('append_copies',200)]:
        data=batch(); data['duplicate_policy']=policy
        response=client.post(route,headers={**manager,'Idempotency-Key':KEY+'-'+policy},json=data)
        assert response.status_code==status
        if policy=='skip':
            assert response.json['skipped_movies']==1 and response.json['created_copies']==0
        if policy=='append_copies':
            assert response.json['created_movies']==0 and response.json['created_copies']==1
    assert client.get('/api/v1/movies').json['total']==1
    assert len(client.get('/api/v1/copies').json['copies'])==2


def test_csv_export_and_reimport_and_formula_neutralization(enterprise):
    client,path,manager,member=context(enterprise)
    client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY},json=batch())
    exported=client.get('/api/v1/catalogue/export?format=csv',headers=manager)
    assert exported.status_code==200 and exported.json['format']=='csv'
    from app.catalogue_transfer import parse_csv
    parsed=parse_csv(exported.json['content'])
    assert parsed['movies'][0]['title']=='Imported film'
    assert len(parsed['movies'][0]['copies'])==1
    formula=batch('=DANGEROUS()')
    client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+'formula'},json=formula)
    exported=client.get('/api/v1/catalogue/export?format=csv',headers=manager)
    assert "'=DANGEROUS()" in exported.json['content']


def test_transfer_rejects_private_fields_and_malformed_csv(enterprise):
    client,path,manager,member=context(enterprise)
    for index,data in enumerate([{'format':'csv','content':'wrong,headers\na,b','duplicate_policy':'reject'},
                                 {'format':'json','content':{'format_version':1,'movies':[],'users':[]},'duplicate_policy':'reject'}]):
        assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+str(index)},json=data).status_code==422


def test_roundtrip_preserves_ids_accents_quotes_and_multiline(enterprise,tmp_path):
    from app.catalogue_transfer import export_csv,normalize,parse_csv
    client,path,manager,member=context(enterprise)
    data=batch('Été, "cinéma"')
    data['content']['movies'][0]['synopsis']='Première ligne\nDeuxième, "citation"'
    data['content']['movies'][0]['actors']=['Élodie, Martin']
    data['content']['movies'][0]['genres']=['Drame']
    assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY},json=data).status_code==200
    exported=client.get('/api/v1/catalogue/export',headers=manager).json['content']
    assert normalize(parse_csv(export_csv(exported)))==exported['movies']
    from app import create_app
    from app.storage_v2 import V2Store
    second=tmp_path/'second.json'; V2Store(second).initialize()
    app=create_app({'TESTING':True,'DATA_FILE':str(tmp_path/'legacy2.json'),'ENTERPRISE_ENABLED':True,
        'ENTERPRISE_DATA_FILE':str(second),'AUTH_THROTTLE_KEY':b'x'*32})
    other=app.test_client()
    _,auth=account(other,'secondmanager','manager',second)
    response=other.post('/api/v1/catalogue/import',headers={**auth,'Idempotency-Key':KEY},json={'format':'csv','content':export_csv(exported)})
    assert response.status_code==200
    assert other.get('/api/v1/catalogue/export',headers=auth).json['content']==exported


def test_import_limits_and_ordinary_limits_stay_separate(enterprise):
    client,path,manager,member=context(enterprise)
    # A valid batch larger than the ordinary 64-KiB cap is accepted.
    data=batch()
    data['content']['movies']=[{**MOVIE,'title':'Film '+str(i),'synopsis':'é'*500,'copies':[]} for i in range(80)]
    response=client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY},json=data)
    assert response.status_code==200 and response.json['created_movies']==80
    assert client.post('/api/v1/movies',headers=manager,json={**MOVIE,'synopsis':'a'*70000}).status_code==413
    assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+'large'},json={'format':'json','content':' '*1048577}).status_code==422
    assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+'http'},data=b' '*(8*1024*1024+1),content_type='application/json').status_code==413
    for content in ({'format_version':1,'movies':[batch()['content']['movies'][0]]*1001},
                    {'format_version':1,'movies':[{**MOVIE,'copies':[COPY]*5001}]}):
        assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+'count'},json={'format':'json','content':content}).status_code==422
    for content in ('{"format_version":1,"format_version":1,"movies":[]}',
                    '{"format_version":1,"movies":NaN}', '['*10000, '\ud800'):
        assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+'bad'},json={'format':'json','content':content}).status_code==422


def import_in_process(path,auth,data,queue):
    from app import create_app
    from pathlib import Path
    app=create_app({'TESTING':True,'DATA_FILE':str(Path(path).parent/'legacy.json'),'ENTERPRISE_ENABLED':True,
        'ENTERPRISE_DATA_FILE':str(path),'AUTH_THROTTLE_KEY':b'x'*32})
    response=app.test_client().post('/api/v1/catalogue/import',headers={**auth,'Idempotency-Key':KEY},json=data)
    queue.put((response.status_code,response.json))


def test_concurrent_imports_create_one_batch_one_receipt_one_audit(enterprise):
    import multiprocessing
    from app.storage_v2 import V2Store
    client,path,manager,member=context(enterprise)
    context_=multiprocessing.get_context('spawn'); queue=context_.Queue()
    processes=[context_.Process(target=import_in_process,args=(path,manager,batch(),queue)) for _ in range(2)]
    for process in processes: process.start()
    for process in processes:
        process.join(25)
        if process.is_alive():
            process.terminate(); process.join()
            raise AssertionError('Import process timed out')
        assert process.exitcode==0
    results=[queue.get(timeout=5) for _ in processes]
    assert results[0]==results[1] and results[0][0]==200
    document=V2Store(path).read()
    assert len(document['movies'])==len(document['copies'])==len(document['idempotency_records'])==1
    assert len([e for e in document['audit_events'] if e['action']=='import.completed'])==1
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/catalogue-v2.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator(schema).validate(document)


def test_conflict_late_in_batch_keeps_catalogue_unchanged(enterprise):
    from app.storage_v2 import V2Store
    client,path,manager,member=context(enterprise)
    assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY},json=batch()).status_code==200
    before=V2Store(path).read()
    data=batch('A new film')
    data['content']['movies'].append(batch()['content']['movies'][0])
    response=client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+'conflict'},json=data)
    assert response.status_code==409
    after=V2Store(path).read()
    assert after['movies']==before['movies'] and after['copies']==before['copies']
    assert len(after['idempotency_records'])==1
    assert after['audit_events'][-1]['action']=='import.failed'
    # Imported IDs must not collide with an account, even if syntactically valid.
    data=batch('A new film'); data['content']['movies'][0]['id']=after['users'][0]['id']
    assert client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY+'collision'},json=data).status_code==409


def test_transfer_example_schema_and_receipt_secret_rejection(enterprise):
    import copy,json
    from pathlib import Path
    import pytest
    from jsonschema import Draft202012Validator
    from app.storage_v2 import V2Store,validate_document
    from app.catalogue_transfer import normalize,parse_csv
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/catalogue-transfer-v1.schema.json').read_text(encoding='utf-8'))
    example=json.loads((root/'schemas/catalogue-transfer-v1.example.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(example)
    assert normalize(example)[0]['copies']
    assert normalize(parse_csv((root/'schemas/catalogue-transfer-v1.example.csv').read_text(encoding='utf-8')))==normalize(example)
    client,path,manager,member=context(enterprise)
    client.post('/api/v1/catalogue/import',headers={**manager,'Idempotency-Key':KEY},json=batch())
    doc=V2Store(path).read()
    for extra in ({'token':'must-not-be-stored'},{'created_movies':True},{'created_copies':5001}):
        tampered=copy.deepcopy(doc); tampered['idempotency_records'][0]['response'].update(extra)
        with pytest.raises(ValueError): validate_document(tampered)
