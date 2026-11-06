import multiprocessing
import time

from test_enterprise_auth import enterprise, account
from test_enterprise_catalogue import MOVIE,COPY


def setup_lending(enterprise):
    client,path = enterprise
    _,manager = account(client,'loanmanager','manager',path)
    movie = client.post('/api/v1/movies',headers=manager,json={**MOVIE,'initial_copy':COPY}).json
    copy = client.get('/api/v1/copies',query_string={'movie_id':movie['id']}).json['copies'][0]
    _,first = account(client,'firstborrower')
    _,second = account(client,'secondborrower')
    return client,path,movie,copy,manager,first,second


def test_loan_lifecycle_permissions_and_private_history(enterprise):
    client,path,movie,copy,manager,first,second = setup_lending(enterprise)
    assert client.post('/api/v1/loans',json={'movie_id':movie['id']}).status_code == 401
    response = client.post('/api/v1/loans',headers=first,json={'movie_id':movie['id']})
    assert response.status_code == 201
    loan = response.json
    assert loan['status'] == 'pending' and loan['copy_id'] is None
    endpoint = '/api/v1/loans/'+loan['id']
    options=client.get(endpoint+'/options',headers=manager)
    assert options.status_code==200 and options.json['copies'][0]['id']==copy['id']
    assert client.get(endpoint+'/options',headers=first).status_code==403
    assert client.get(endpoint,headers=second).status_code == 404
    assert client.get('/api/v1/loans',headers=second).json['total'] == 0
    assert client.post('/api/v1/loans',headers=first,json={'movie_id':movie['id']}).status_code == 409
    body = {'copy_id':copy['id'],'due_at':int(time.time())+86400}
    assert client.post(endpoint+'/approve',headers={**first,'If-Match':loan['_etag']},json=body).status_code == 403
    assert client.post(endpoint+'/approve',headers=manager,json=body).status_code == 428
    approved = client.post(endpoint+'/approve',headers={**manager,'If-Match':loan['_etag']},json=body)
    assert approved.status_code == 200 and approved.json['status'] == 'active'
    assert client.post(endpoint+'/cancel',headers={**first,'If-Match':approved.json['_etag']}).status_code == 409
    assert client.post(endpoint+'/return',headers={**first,'If-Match':approved.json['_etag']}).status_code == 403
    returned = client.post(endpoint+'/return',headers={**manager,'If-Match':approved.json['_etag']})
    assert returned.status_code == 200 and returned.json['status'] == 'returned'
    assert returned.json['returned_at'] >= returned.json['approved_at']
    assert client.delete('/api/v1/copies/'+copy['id'],headers={**manager,'If-Match':copy['_etag']}).status_code == 409
    assert client.get(endpoint,headers=first).json['status'] == 'returned'


def test_pending_cancellation_invalid_dates_and_unloanable_copy(enterprise):
    client,path,movie,copy,manager,first,second = setup_lending(enterprise)
    loan = client.post('/api/v1/loans',headers=first,json={'movie_id':movie['id']}).json
    endpoint = '/api/v1/loans/'+loan['id']
    assert client.post(endpoint+'/cancel',headers={**second,'If-Match':loan['_etag']}).status_code == 404
    for due in (True,int(time.time())-1,int(time.time())+100*86400):
        assert client.post(endpoint+'/approve',headers={**manager,'If-Match':loan['_etag']},json={'copy_id':copy['id'],'due_at':due}).status_code == 422
    changed = client.put('/api/v1/copies/'+copy['id'],headers={**manager,'If-Match':copy['_etag']},json={**COPY,'condition':'damaged'})
    assert changed.status_code == 200
    assert client.post(endpoint+'/approve',headers={**manager,'If-Match':loan['_etag']},json={'copy_id':copy['id'],'due_at':int(time.time())+86400}).status_code == 409
    cancelled = client.post(endpoint+'/cancel',headers={**first,'If-Match':loan['_etag']})
    assert cancelled.status_code == 200 and cancelled.json['status'] == 'cancelled'
    assert client.post(endpoint+'/cancel',headers={**first,'If-Match':loan['_etag']}).status_code == 412


def approve_in_process(path,authorization,loan,copy_id,queue):
    from pathlib import Path
    from app import create_app
    app = create_app({'TESTING':True,'DATA_FILE':str(Path(path).parent/'legacy.json'),
                      'ENTERPRISE_ENABLED':True,'ENTERPRISE_DATA_FILE':str(path),'AUTH_THROTTLE_KEY':b'x'*32})
    response = app.test_client().post('/api/v1/loans/'+loan['id']+'/approve',
        headers={**authorization,'If-Match':loan['_etag']},json={'copy_id':copy_id,'due_at':int(time.time())+86400})
    queue.put(response.status_code)


def test_two_processes_cannot_approve_the_same_copy(enterprise):
    from app.storage_v2 import V2Store
    client,path,movie,copy,manager,first,second = setup_lending(enterprise)
    loans = [client.post('/api/v1/loans',headers=member,json={'movie_id':movie['id']}).json for member in (first,second)]
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    processes = [context.Process(target=approve_in_process,args=(path,manager,loan,copy['id'],queue)) for loan in loans]
    for process in processes:
        process.start()
    for process in processes:
        process.join(25)
        if process.is_alive():
            process.terminate()
            process.join()
            raise AssertionError('Concurrent approval process timed out')
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=5) for _ in processes) == [200,409]
    document = V2Store(path).read()
    assert len([loan for loan in document['loans'] if loan['status'] == 'active' and loan['copy_id'] == copy['id']]) == 1


def test_availability_overdue_and_loan_schemas(enterprise):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from app.storage_v2 import V2Store
    client,path,movie,copy,manager,first,second = setup_lending(enterprise)
    timestamp=int(time.time())
    client.application.config['AUTH_CLOCK']=lambda:timestamp
    loan=client.post('/api/v1/loans',headers=first,json={'movie_id':movie['id']}).json
    assert client.get('/api/v1/availability',query_string={'movie_id':movie['id']}).json['available']==1
    approved=client.post('/api/v1/loans/'+loan['id']+'/approve',headers={**manager,'If-Match':loan['_etag']},json={'copy_id':copy['id'],'due_at':timestamp+60})
    assert approved.status_code==200
    client.application.config['AUTH_CLOCK']=lambda:timestamp+61
    listing=client.get('/api/v1/loans',headers=first).json
    assert listing['overdue_ids']==[loan['id']]
    available=client.get('/api/v1/availability',query_string={'movie_id':movie['id']}).json
    assert available['available']==0 and available['loaned']==1
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/catalogue-v2.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(V2Store(path).read())
    spec=json.loads((root/'docs/openapi-v1.json').read_text(encoding='utf-8'))
    for data,name in [(approved.json,'Loan'),(listing,'LoanList'),(available,'Availability')]:
        Draft202012Validator({'$ref':'#/components/schemas/'+name,'components':spec['components']}).validate(data)


def test_disabled_borrower_cannot_receive_a_pending_loan(enterprise):
    from app.storage_v2 import V2Store
    client,path,movie,copy,manager,first,second=setup_lending(enterprise)
    loan=client.post('/api/v1/loans',headers=first,json={'movie_id':movie['id']}).json
    V2Store(path).transact(lambda document: next(u for u in document['users'] if u['id']==loan['borrower_id']).update(enabled=False))
    assert client.get('/api/v1/loans',headers=first).status_code==401
    response=client.post('/api/v1/loans/'+loan['id']+'/approve',headers={**manager,'If-Match':loan['_etag']},
                         json={'copy_id':copy['id'],'due_at':int(time.time())+86400})
    assert response.status_code==409
