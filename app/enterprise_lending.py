"""Loan state machine; approval and inventory checks share one file transaction."""
import uuid

from flask import Blueprint,request,url_for

from .audit import record_event
from .enterprise_auth import check_store,error,forbidden,no_cache,now,payload,protected,public_profile
from .enterprise_catalogue import bounded_integer,lookup,missing,precondition,resource,respond
from .validation import ValidationError
from .reservation_service import availability as inventory_availability,expire_waiting,free_copies,fulfill,priority_allows

bp = Blueprint('enterprise_lending',__name__,url_prefix='/api/v1')
bp.after_request(no_cache)
MANAGERS = ('manager','admin')


def visible_loan(document,user,identity):
    loan = lookup(document,'loans',identity)
    return loan if loan and (loan['borrower_id'] == user['id'] or user['role'] in MANAGERS) else None


def borrower_problem(document,loan):
    borrower=lookup(document,'users',loan['borrower_id'])
    if borrower is None or not borrower['enabled']:
        return error(409,'borrower_disabled','Ce compte ne peut pas recevoir un nouveau prêt.')
    if now()<loan['requested_at']:
        return error(409,'clock_state','Horloge incohérente avec la demande.')
    if sum(l['borrower_id']==loan['borrower_id'] and l['status']=='active' for l in document['loans'])>=5:
        return error(409,'loan_limit','Limite de prêts actifs atteinte.')
    return None


@bp.post('/loans')
def request_loan():
    def create(document,user,session):
        data = payload(('movie_id',))
        if not isinstance(data['movie_id'],str):
            raise ValidationError({'movie_id':'Identifiant de film invalide.'})
        if lookup(document,'movies',data['movie_id']) is None:
            return missing()
        owned = [l for l in document['loans'] if l['borrower_id'] == user['id']]
        if any(l['movie_id'] == data['movie_id'] and l['status'] in ('pending','active') for l in owned):
            return error(409,'open_loan','Une demande ou un prêt existe déjà pour ce film.')
        if sum(l['status'] == 'pending' for l in owned) >= 10:
            return error(409,'loan_limit','Limite de demandes atteinte.')
        loan = {'id':str(uuid.uuid4()),'movie_id':data['movie_id'],'borrower_id':user['id'],
                'copy_id':None,'status':'pending','requested_at':now(),'approved_at':None,'due_at':None,'returned_at':None,'cancelled_at':None}
        document['loans'].append(loan)
        record_event(document,user['id'],'loan',loan['id'],'requested')
        return respond(loan,201,url_for('enterprise_lending.get_loan',loan_id=loan['id']))
    return protected(create)


@bp.get('/loans')
def list_loans():
    def listing(document,user,session):
        if set(request.args) - {'status','limit','offset','scope','movie_id'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
            raise ValidationError({'query':'Paramètres invalides.'})
        status = request.args.get('status','')
        if status not in ('','open','pending','active','returned','cancelled'):
            raise ValidationError({'status':'Statut invalide.'})
        scope=request.args.get('scope','all' if user['role'] in MANAGERS else 'mine')
        if scope not in ('mine','all'):
            raise ValidationError({'scope':'Périmètre invalide.'})
        if scope=='all' and user['role'] not in MANAGERS:
            return forbidden()
        movie_filter=request.args.get('movie_id','')
        limit,offset = bounded_integer('limit',24,1,100),bounded_integer('offset',0,0,10000)
        loans = [l for l in document['loans'] if (scope=='all' or l['borrower_id']==user['id'])
            and (not status or l['status']==status or status=='open' and l['status'] in ('pending','active'))
            and (not movie_filter or l['movie_id']==movie_filter)]
        loans.sort(key=lambda l:(l['requested_at'],l['id']),reverse=True)
        page = loans[offset:offset+limit]
        movie_ids = {l['movie_id'] for l in page}
        borrower_ids = {l['borrower_id'] for l in page}
        return {'loans':[resource(l) for l in page],'total':len(loans),'limit':limit,'offset':offset,
            'movies':{m['id']:resource(m) for m in document['movies'] if m['id'] in movie_ids},
            'borrowers':{u['id']:public_profile(u) for u in document['users'] if u['id'] in borrower_ids},
            'overdue_ids':[l['id'] for l in page if l['status'] == 'active' and l['due_at'] < now()]}
    return protected(listing)


@bp.get('/loans/<loan_id>')
def get_loan(loan_id):
    def get(document,user,session):
        loan = visible_loan(document,user,loan_id)
        return missing() if loan is None else respond(loan)
    return protected(get)


@bp.post('/loans/<loan_id>/approve')
def approve_loan(loan_id):
    def approve(document,user,session):
        if user['role'] not in MANAGERS:
            return forbidden()
        expire_waiting(document,now())
        loan = lookup(document,'loans',loan_id)
        if loan is None:
            return missing()
        conflict = precondition(loan)
        if conflict:
            return conflict
        if loan['status'] != 'pending':
            return error(409,'loan_state','Seule une demande en attente peut être validée.')
        problem=borrower_problem(document,loan)
        if problem:
            return problem
        data = payload(('copy_id','due_at'))
        timestamp = now()
        if not isinstance(data['copy_id'],str) or type(data['due_at']) is not int or not timestamp < data['due_at'] <= timestamp+90*86400:
            raise ValidationError({'body':'Exemplaire ou échéance invalide.'})
        copy = lookup(document,'copies',data['copy_id'])
        if copy is None:
            return missing()
        if copy['movie_id'] != loan['movie_id'] or copy['condition'] == 'damaged' or any(l['copy_id'] == copy['id'] and l['status'] == 'active' for l in document['loans']):
            return error(409,'copy_unavailable','Cet exemplaire ne peut pas être attribué.')
        if not priority_allows(document,loan,timestamp):
            return error(409,'reservation_priority','Une réservation prioritaire doit être traitée avant cette demande.')
        loan.update(copy_id=copy['id'],status='active',approved_at=timestamp,due_at=data['due_at'])
        fulfill(document,loan,user['id'],timestamp)
        record_event(document,user['id'],'loan',loan_id,'approved')
        return respond(loan)
    return protected(approve)


@bp.get('/loans/<loan_id>/options')
def loan_options(loan_id):
    def options(document,user,session):
        if user['role'] not in MANAGERS:
            return forbidden()
        expire_waiting(document,now())
        loan=lookup(document,'loans',loan_id)
        if loan is None:
            return missing()
        if loan['status']!='pending':
            return error(409,'loan_state','Cette demande n’est plus en attente.')
        problem=borrower_problem(document,loan)
        if problem:
            return problem
        copies=free_copies(document,loan['movie_id']) if priority_allows(document,loan,now()) else []
        return {'loan_id':loan_id,'copies':[resource(c) for c in copies]}
    return protected(options)


@bp.post('/loans/<loan_id>/return')
def return_loan(loan_id):
    def record_return(document,user,session):
        if request.get_data():
            raise ValidationError({'body':'Aucun corps attendu.'})
        if user['role'] not in MANAGERS:
            return forbidden()
        loan = lookup(document,'loans',loan_id)
        if loan is None:
            return missing()
        conflict = precondition(loan)
        if conflict:
            return conflict
        if loan['status'] != 'active' or now() < loan['approved_at']:
            return error(409,'loan_state','Ce prêt ne peut pas être retourné.')
        loan.update(status='returned',returned_at=now())
        record_event(document,user['id'],'loan',loan_id,'returned')
        return respond(loan)
    return protected(record_return)


@bp.post('/loans/<loan_id>/cancel')
def cancel_loan(loan_id):
    def cancel(document,user,session):
        if request.get_data():
            raise ValidationError({'body':'Aucun corps attendu.'})
        loan = visible_loan(document,user,loan_id)
        if loan is None:
            return missing()
        conflict = precondition(loan)
        if conflict:
            return conflict
        if loan['status'] != 'pending' or now() < loan['requested_at']:
            return error(409,'loan_state','Seule une demande en attente peut être annulée.')
        loan.update(status='cancelled',cancelled_at=now())
        record_event(document,user['id'],'loan',loan_id,'cancelled')
        return respond(loan)
    return protected(cancel)


@bp.get('/availability')
def availability():
    if set(request.args) != {'movie_id'} or len(request.args.getlist('movie_id')) != 1:
        raise ValidationError({'movie_id':'Film requis.'})
    identity = request.args['movie_id']
    document = check_store()
    if lookup(document,'movies',identity) is None:
        return missing()
    return inventory_availability(document,identity,now())
