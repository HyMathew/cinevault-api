import uuid

from flask import Blueprint,request,url_for

from .audit import record_event
from .enterprise_auth import error,forbidden,no_cache,now,payload,protected,public_profile
from .enterprise_catalogue import bounded_integer,lookup,missing,precondition,resource,respond
from .reservation_service import availability,expire_waiting,free_copies,waiting
from .validation import ValidationError

bp=Blueprint('enterprise_reservations',__name__,url_prefix='/api/v1')
bp.after_request(no_cache)
MANAGERS=('manager','admin')


def visible(document,user,identity):
    entry=lookup(document,'reservations',identity)
    return entry if entry and (entry['user_id']==user['id'] or user['role'] in MANAGERS) else None


@bp.post('/reservations')
def create_reservation():
    def create(document,user,session):
        timestamp=now()
        expire_waiting(document,timestamp)
        data=payload(('movie_id',))
        if not isinstance(data['movie_id'],str):
            raise ValidationError({'movie_id':'Identifiant de film invalide.'})
        movie_id=data['movie_id']
        if lookup(document,'movies',movie_id) is None:
            return missing()
        owned=[r for r in document['reservations'] if r['user_id']==user['id'] and r['status']=='waiting']
        if any(r['movie_id']==movie_id for r in owned):
            return error(409,'duplicate','Vous attendez déjà ce film.')
        if len(owned)>=10:
            return error(409,'reservation_limit','Limite de réservations en attente atteinte.')
        if any(l['movie_id']==movie_id and l['borrower_id']==user['id'] and l['status']=='active' for l in document['loans']):
            return error(409,'active_loan','Vous avez déjà emprunté ce film.')
        if availability(document,movie_id,timestamp)['available']>0:
            return error(409,'copy_available','Un exemplaire est disponible : demandez un prêt.')
        entry={'id':str(uuid.uuid4()),'movie_id':movie_id,'user_id':user['id'],'status':'waiting',
               'sequence':document['revision']+1,'created_at':timestamp,'expires_at':timestamp+14*86400,'closed_at':None,'loan_id':None}
        document['reservations'].append(entry)
        record_event(document,user['id'],'reservation',entry['id'],'created')
        return respond(entry,201,url_for('enterprise_reservations.get_reservation',reservation_id=entry['id']))
    return protected(create)


@bp.get('/reservations')
def list_reservations():
    def listing(document,user,session):
        timestamp=now()
        expire_waiting(document,timestamp)
        if set(request.args)-{'status','limit','offset','scope','movie_id'} or any(len(request.args.getlist(k))!=1 for k in request.args):
            raise ValidationError({'query':'Paramètres invalides.'})
        status=request.args.get('status','')
        if status not in ('','waiting','fulfilled','cancelled','expired'):
            raise ValidationError({'status':'Statut invalide.'})
        scope=request.args.get('scope','all' if user['role'] in MANAGERS else 'mine')
        if scope not in ('mine','all'):
            raise ValidationError({'scope':'Périmètre invalide.'})
        if scope=='all' and user['role'] not in MANAGERS:
            return forbidden()
        movie_filter=request.args.get('movie_id','')
        limit,offset=bounded_integer('limit',24,1,100),bounded_integer('offset',0,0,10000)
        entries=[r for r in document['reservations'] if (scope=='all' or r['user_id']==user['id']) and (not status or r['status']==status) and (not movie_filter or r['movie_id']==movie_filter)]
        entries.sort(key=lambda r:r['sequence'],reverse=True)
        page=entries[offset:offset+limit]
        movie_ids={r['movie_id'] for r in page}
        user_ids={r['user_id'] for r in page}
        positions={}
        ready=[]
        for identity in movie_ids:
            queue=waiting(document,identity,timestamp)
            for index,entry in enumerate(queue,1):
                if entry in page:
                    positions[entry['id']]=index
            if queue and queue[0] in page and free_copies(document,identity):
                ready.append(queue[0]['id'])
        open_loans={(l['borrower_id'],l['movie_id']):l['id'] for l in document['loans'] if l['status'] in ('pending','active')}
        return {'reservations':[resource(r) for r in page],'total':len(entries),'limit':limit,'offset':offset,
            'movies':{m['id']:resource(m) for m in document['movies'] if m['id'] in movie_ids},
            'users':{u['id']:public_profile(u) for u in document['users'] if u['id'] in user_ids},
            'positions':positions,'ready_ids':ready,
            'open_loan_ids':{r['id']:open_loans[(r['user_id'],r['movie_id'])] for r in page if (r['user_id'],r['movie_id']) in open_loans}}
    return protected(listing)


@bp.get('/reservations/<reservation_id>')
def get_reservation(reservation_id):
    def get(document,user,session):
        expire_waiting(document,now())
        entry=visible(document,user,reservation_id)
        return missing() if entry is None else respond(entry)
    return protected(get)


@bp.post('/reservations/<reservation_id>/cancel')
def cancel_reservation(reservation_id):
    def cancel(document,user,session):
        expire_waiting(document,now())
        if request.get_data():
            raise ValidationError({'body':'Aucun corps attendu.'})
        entry=visible(document,user,reservation_id)
        if entry is None:
            return missing()
        conflict=precondition(entry)
        if conflict:
            return conflict
        if entry['status']!='waiting' or now()<entry['created_at']:
            return error(409,'reservation_state','Cette réservation ne peut pas être annulée.')
        entry.update(status='cancelled',closed_at=now())
        record_event(document,user['id'],'reservation',entry['id'],'cancelled')
        return respond(entry)
    return protected(cancel)
