import json
from pathlib import Path
import time

from jsonschema import Draft202012Validator

from test_enterprise_auth import enterprise,account
from test_loans import setup_lending


def occupied(enterprise):
    client,path,movie,copy,manager,first,second=setup_lending(enterprise)
    loan=client.post('/api/v1/loans',headers=first,json={'movie_id':movie['id']}).json
    loan=client.post('/api/v1/loans/'+loan['id']+'/approve',headers={**manager,'If-Match':loan['_etag']},
                     json={'copy_id':copy['id'],'due_at':int(time.time())+86400}).json
    return client,path,movie,copy,manager,first,second,loan


def login(client,name):
    response=client.post('/api/v1/auth/login',json={'username':name,'password':'test passphrase 123'})
    assert response.status_code==200
    return {'Authorization':'Bearer '+response.json['token']}


def test_reservation_requires_unavailable_inventory_and_owner_identity(enterprise):
    client,path,movie,copy,manager,first,second=setup_lending(enterprise)
    assert client.post('/api/v1/reservations',headers=second,json={'movie_id':movie['id']}).status_code==409
    loan=client.post('/api/v1/loans',headers=first,json={'movie_id':movie['id']}).json
    client.post('/api/v1/loans/'+loan['id']+'/approve',headers={**manager,'If-Match':loan['_etag']},json={'copy_id':copy['id'],'due_at':int(time.time())+86400})
    assert client.post('/api/v1/reservations',json={'movie_id':movie['id']}).status_code==401
    assert client.post('/api/v1/reservations',headers=second,json={'movie_id':movie['id'],'user_id':'other'}).status_code==422
    response=client.post('/api/v1/reservations',headers=second,json={'movie_id':movie['id']})
    assert response.status_code==201
    reservation=response.json
    assert reservation['status']=='waiting'
    assert client.get('/api/v1/reservations',headers=second,query_string={'scope':'all'}).status_code==403
    assert client.get('/api/v1/reservations',headers=manager,query_string={'scope':'mine','movie_id':movie['id']}).json['total']==0
    assert client.post('/api/v1/reservations',headers=second,json={'movie_id':movie['id']}).status_code==409
    assert client.get('/api/v1/reservations/'+reservation['id'],headers=first).status_code==404
    assert client.post('/api/v1/reservations/'+reservation['id']+'/cancel',headers={**first,'If-Match':reservation['_etag']}).status_code==404


def test_fifo_priority_fulfillment_and_cancellation(enterprise):
    client,path,movie,copy,manager,first,second,loan=occupied(enterprise)
    _,third=account(client,'thirdborrower')
    second_res=client.post('/api/v1/reservations',headers=second,json={'movie_id':movie['id']}).json
    third_res=client.post('/api/v1/reservations',headers=third,json={'movie_id':movie['id']}).json
    assert second_res['sequence']<third_res['sequence']
    listing=client.get('/api/v1/reservations',headers=third).json
    assert listing['total']==1 and listing['positions'][third_res['id']]==2
    assert b'secondborrower' not in client.get('/api/v1/reservations',headers=third).data
    client.post('/api/v1/loans/'+loan['id']+'/return',headers={**manager,'If-Match':loan['_etag']})
    available=client.get('/api/v1/availability',query_string={'movie_id':movie['id']}).json
    assert available['free']==1 and available['available']==0 and available['reserved']==2
    third_loan=client.post('/api/v1/loans',headers=third,json={'movie_id':movie['id']}).json
    second_loan=client.post('/api/v1/loans',headers=second,json={'movie_id':movie['id']}).json
    assert client.get('/api/v1/reservations',headers=second).json['open_loan_ids'][second_res['id']]==second_loan['id']
    body={'copy_id':copy['id'],'due_at':int(time.time())+86400}
    assert client.post('/api/v1/loans/'+third_loan['id']+'/approve',headers={**manager,'If-Match':third_loan['_etag']},json=body).status_code==409
    assert client.get('/api/v1/loans/'+third_loan['id']+'/options',headers=manager).json['copies']==[]
    approved=client.post('/api/v1/loans/'+second_loan['id']+'/approve',headers={**manager,'If-Match':second_loan['_etag']},json=body)
    assert approved.status_code==200
    fulfilled=client.get('/api/v1/reservations/'+second_res['id'],headers=second).json
    assert fulfilled['status']=='fulfilled' and fulfilled['loan_id']==second_loan['id']
    assert client.get('/api/v1/reservations',headers=third).json['positions'][third_res['id']]==1
    assert client.post('/api/v1/reservations/'+third_res['id']+'/cancel',headers=third).status_code==428
    cancelled=client.post('/api/v1/reservations/'+third_res['id']+'/cancel',headers={**third,'If-Match':third_res['_etag']})
    assert cancelled.status_code==200 and cancelled.json['status']=='cancelled'


def test_expiration_does_not_hold_inventory_and_uses_system_actor(enterprise):
    from app.storage_v2 import V2Store
    client,path,movie,copy,manager,first,second,loan=occupied(enterprise)
    timestamp=int(time.time())
    client.application.config['AUTH_CLOCK']=lambda:timestamp
    reservation=client.post('/api/v1/reservations',headers=second,json={'movie_id':movie['id']}).json
    client.application.config['AUTH_CLOCK']=lambda:reservation['expires_at']+1
    second=login(client,'secondborrower')
    manager=login(client,'loanmanager')
    expired=client.get('/api/v1/reservations/'+reservation['id'],headers=second)
    assert expired.status_code==200 and expired.json['status']=='expired'
    assert client.post('/api/v1/loans/'+loan['id']+'/return',headers={**manager,'If-Match':loan['_etag']}).status_code==200
    assert client.get('/api/v1/availability',query_string={'movie_id':movie['id']}).json['available']==1
    document=V2Store(path).read()
    event=next(e for e in document['audit_events'] if e['action']=='reservation.expired')
    assert event['actor_id'] is None
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/catalogue-v2.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    spec=json.loads((root/'docs/openapi-v1.json').read_text(encoding='utf-8'))
    for data,name in [(expired.json,'Reservation'),(client.get('/api/v1/reservations',headers=second).json,'ReservationList')]:
        Draft202012Validator({'$ref':'#/components/schemas/'+name,'components':spec['components']}).validate(data)


def test_disabling_account_cancels_waiting_reservations(enterprise):
    client,path,movie,copy,manager,first,second,loan=occupied(enterprise)
    _,admin=account(client,'reservationadmin','admin',path)
    reservation=client.post('/api/v1/reservations',headers=second,json={'movie_id':movie['id']}).json
    assert client.patch('/api/v1/users/'+reservation['user_id'],headers=admin,json={'enabled':False}).status_code==200
    observed=client.get('/api/v1/reservations/'+reservation['id'],headers=manager)
    assert observed.status_code==200 and observed.json['status']=='cancelled'


def reserve_in_process(path,authorization,movie_id,queue):
    from app import create_app
    app=create_app({'TESTING':True,'DATA_FILE':str(Path(path).parent/'legacy.json'),
        'ENTERPRISE_ENABLED':True,'ENTERPRISE_DATA_FILE':str(path),'AUTH_THROTTLE_KEY':b'x'*32})
    response=app.test_client().post('/api/v1/reservations',headers=authorization,json={'movie_id':movie_id})
    queue.put(response.status_code)


def test_two_processes_cannot_duplicate_a_waiting_reservation(enterprise):
    import multiprocessing
    from app.storage_v2 import V2Store
    client,path,movie,copy,manager,first,second,loan=occupied(enterprise)
    context=multiprocessing.get_context('spawn')
    queue=context.Queue()
    processes=[context.Process(target=reserve_in_process,args=(path,second,movie['id'],queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(25)
        if process.is_alive():
            process.terminate()
            process.join()
            raise AssertionError('Reservation process timed out')
        assert process.exitcode==0
    assert sorted(queue.get(timeout=5) for _ in processes)==[201,409]
    document=V2Store(path).read()
    assert len(document['reservations'])==1
    assert len([e for e in document['audit_events'] if e['action']=='reservation.created'])==1
