"""Cross-reference validation for the implemented private collection subset."""
from .validation import SUPPORTS, is_unicode

KINDS = ('owned','favorite','watched','watchlist','custom')
AUDIT_TYPES = ('movie','copy','collection','loan','reservation','import','export')
AUDIT_ACTIONS = tuple(f'{kind}.{action}' for kind in ('movie','copy','collection') for action in ('created','updated','deleted')) + tuple('loan.'+action for action in ('requested','approved','returned','cancelled')) + tuple('reservation.'+action for action in ('created','fulfilled','cancelled','expired')) + ('import.completed','import.failed','export.completed')


def valid_collection_attributes(kind, name, supports):
    if kind not in KINDS or not isinstance(name,str) or not is_unicode(name):
        return False
    if (kind == 'custom' and (not 1 <= len(name) <= 80 or name.strip() != name or any(ord(c) < 32 for c in name))) or (kind != 'custom' and name != ''):
        return False
    if not isinstance(supports,list) or len(supports) > 5 or any(not isinstance(s,str) or s not in SUPPORTS for s in supports) or len(set(supports)) != len(supports):
        return False
    return kind == 'owned' or supports == []


def validate_business(document, ids, valid_uuid):
    if len(document['collections']) > 10000 or len(document['loans']) > 10000 or len(document['reservations']) > 10000 or len(document['audit_events']) > 2000:
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
    copies = {c['id']:c for c in document['copies']}
    active_copies,open_pairs = set(),set()
    pending_counts,active_counts = {},{}
    for loan in document['loans']:
        if not isinstance(loan,dict) or set(loan) != {'id','movie_id','borrower_id','copy_id','status','requested_at','approved_at','due_at','returned_at','cancelled_at'}:
            raise ValueError('Invalid loan fields')
        if not valid_uuid(loan['id']) or loan['id'] in ids or loan['movie_id'] not in movies or loan['borrower_id'] not in users:
            raise ValueError('Invalid loan reference')
        if loan['status'] not in ('pending','active','returned','cancelled') or type(loan['requested_at']) is not int or loan['requested_at'] < 0:
            raise ValueError('Invalid loan state')
        for key in ('approved_at','due_at','returned_at','cancelled_at'):
            if loan[key] is not None and (type(loan[key]) is not int or loan[key] < 0):
                raise ValueError('Invalid loan timestamp')
        status = loan['status']
        if status in ('pending','cancelled'):
            if any(loan[key] is not None for key in ('copy_id','approved_at','due_at','returned_at')):
                raise ValueError('Pending loan has an assigned copy')
            if (status == 'pending' and loan['cancelled_at'] is not None) or (status == 'cancelled' and (loan['cancelled_at'] is None or loan['cancelled_at'] < loan['requested_at'])):
                raise ValueError('Invalid cancellation')
        else:
            copy = copies.get(loan['copy_id'])
            if not copy or copy['movie_id'] != loan['movie_id'] or loan['approved_at'] is None or loan['due_at'] is None or not loan['requested_at'] <= loan['approved_at'] < loan['due_at'] <= loan['approved_at']+90*86400 or loan['cancelled_at'] is not None:
                raise ValueError('Invalid assigned loan')
            if (status == 'active' and loan['returned_at'] is not None) or (status == 'returned' and (loan['returned_at'] is None or loan['returned_at'] < loan['approved_at'])):
                raise ValueError('Invalid return')
        borrower = loan['borrower_id']
        if status in ('pending','active'):
            pair = (borrower,loan['movie_id'])
            if pair in open_pairs:
                raise ValueError('Duplicate open loan')
            open_pairs.add(pair)
        if status == 'active':
            if loan['copy_id'] in active_copies:
                raise ValueError('Copy loaned twice')
            active_copies.add(loan['copy_id'])
            active_counts[borrower] = active_counts.get(borrower,0)+1
        if status == 'pending':
            pending_counts[borrower] = pending_counts.get(borrower,0)+1
        ids.add(loan['id'])
    if any(count > 5 for count in active_counts.values()) or any(count > 10 for count in pending_counts.values()):
        raise ValueError('Borrower loan limit exceeded')
    validate_reservations(document,ids,valid_uuid,users,movies)
    for event in document['audit_events']:
        if not isinstance(event,dict) or set(event) != {'id','actor_id','action','entity_type','entity_id','timestamp'}:
            raise ValueError('Invalid audit event fields')
        system_event=event['actor_id'] is None and event['action']=='reservation.expired'
        if not valid_uuid(event['id']) or event['id'] in ids or (not system_event and event['actor_id'] not in users) or not valid_uuid(event['entity_id']):
            raise ValueError('Invalid audit reference')
        if event['entity_type'] not in AUDIT_TYPES or event['action'] not in AUDIT_ACTIONS or not event['action'].startswith(event['entity_type']+'.') or type(event['timestamp']) is not int or event['timestamp'] < 0:
            raise ValueError('Invalid audit event')
        ids.add(event['id'])


def validate_reservations(document,ids,valid_uuid,users,movies):
    loans={l['id']:l for l in document['loans']}
    enabled={u['id'] for u in document['users'] if u['enabled']}
    sequences,open_pairs,fulfilled_loans=set(),set(),set()
    counts={}
    for reservation in document['reservations']:
        if not isinstance(reservation,dict) or set(reservation)!={'id','movie_id','user_id','status','sequence','created_at','expires_at','closed_at','loan_id'}:
            raise ValueError('Invalid reservation fields')
        if not valid_uuid(reservation['id']) or reservation['id'] in ids or reservation['movie_id'] not in movies or reservation['user_id'] not in users:
            raise ValueError('Invalid reservation reference')
        if reservation['status'] not in ('waiting','fulfilled','cancelled','expired') or type(reservation['sequence']) is not int or not 1<=reservation['sequence']<=document['revision'] or reservation['sequence'] in sequences:
            raise ValueError('Invalid reservation sequence or status')
        if any(type(reservation[k]) is not int for k in ('created_at','expires_at')) or not 0<=reservation['created_at']<reservation['expires_at']<=reservation['created_at']+14*86400:
            raise ValueError('Invalid reservation timestamps')
        status=reservation['status']
        if status=='waiting':
            pair=(reservation['user_id'],reservation['movie_id'])
            if reservation['closed_at'] is not None or reservation['loan_id'] is not None or pair in open_pairs or reservation['user_id'] not in enabled:
                raise ValueError('Invalid waiting reservation')
            open_pairs.add(pair)
            counts[reservation['user_id']]=counts.get(reservation['user_id'],0)+1
        else:
            if type(reservation['closed_at']) is not int or reservation['closed_at']<reservation['created_at']:
                raise ValueError('Invalid closed reservation')
            if status=='expired' and reservation['closed_at']<reservation['expires_at']:
                raise ValueError('Reservation expired too early')
            if status=='fulfilled':
                loan=loans.get(reservation['loan_id'])
                if not loan or loan['status'] not in ('active','returned') or loan['movie_id']!=reservation['movie_id'] or loan['borrower_id']!=reservation['user_id'] or loan['approved_at']!=reservation['closed_at'] or reservation['closed_at']>=reservation['expires_at'] or loan['id'] in fulfilled_loans:
                    raise ValueError('Invalid fulfilled reservation')
                fulfilled_loans.add(loan['id'])
            elif reservation['loan_id'] is not None:
                raise ValueError('Unexpected reservation loan reference')
        sequences.add(reservation['sequence'])
        ids.add(reservation['id'])
    if any(count>10 for count in counts.values()):
        raise ValueError('Reservation limit exceeded')
