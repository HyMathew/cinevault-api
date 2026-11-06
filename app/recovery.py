"""Offline, no-clobber recovery with account quarantine and fresh credentials."""
import argparse
import getpass
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sys
import time
import uuid

import portalocker

from .audit import record_event
from .enterprise_auth import hasher,make_user,normalize_username,valid_display,valid_password
from .storage_v2 import V2Store,_encode,_write_new,decode_document,validate_document

TIMES=('created_at','last_seen','requested_at','approved_at','returned_at','cancelled_at','closed_at','timestamp')


def check_clock(document,timestamp):
    if type(timestamp) is not int or timestamp<0 or any(
        type(entry.get(key)) is int and entry[key]>timestamp
        for collection in document.values() if isinstance(collection,list)
        for entry in collection for key in TIMES
    ):
        raise ValueError('Recovery clock precedes stored events')


def verified_snapshot(path,expected_sha256):
    if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256):
        raise ValueError('Invalid expected digest')
    data=Path(path).read_bytes()
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(),expected_sha256):
        raise ValueError('Snapshot digest mismatch')
    return decode_document(data)


def recover(snapshot,target,expected_sha256,username,display_name,password,*,timestamp=None):
    target=Path(target)
    if target.exists(): raise FileExistsError('Recovery target already exists')
    document=verified_snapshot(snapshot,expected_sha256)
    timestamp=int(time.time()) if timestamp is None else timestamp
    check_clock(document,timestamp)
    username=normalize_username(username)
    if not username or not valid_display(display_name) or not valid_password(password) or any(u['username']==username for u in document['users']):
        raise ValueError('Invalid recovery administrator')
    admin=make_user(username,display_name,password,'admin',timestamp)
    report={'operation_id':str(uuid.uuid4()),'source_sha256':expected_sha256,'target_sha256':'',
        'disabled_users':len(document['users']),'revoked_sessions':len(document['sessions']),'closed_reservations':0}
    for user in document['users']:
        user['enabled']=False
        # Discard each independent random password; re-enabling cannot revive an archived password.
        user['password_hash']=hasher.hash(secrets.token_urlsafe(48))
    document['users'].append(admin)
    document['sessions']=[]
    document['auth_attempts']=[]
    for reservation in document['reservations']:
        if reservation['status']!='waiting': continue
        expired=reservation['expires_at']<=timestamp
        reservation.update(status='expired' if expired else 'cancelled',closed_at=timestamp)
        record_event(document,None if expired else admin['id'],'reservation',reservation['id'],
                     'expired' if expired else 'cancelled',timestamp=timestamp)
        report['closed_reservations']+=1
    record_event(document,admin['id'],'recovery',report['operation_id'],'completed',timestamp=timestamp)
    document['revision']+=1
    validate_document(document)
    encoded=_encode(document)
    with portalocker.Lock(str(target)+'.lock',mode='a',timeout=10):
        _write_new(target,encoded)
    report['target_sha256']=hashlib.sha256(encoded).hexdigest()
    return report


def reset_password(store,operator,username,password,*,enable=False,timestamp=None):
    operator,username=normalize_username(operator),normalize_username(username)
    if not operator or not username or not valid_password(password) or type(enable) is not bool:
        raise ValueError('Invalid reset fields')
    password_hash=hasher.hash(password)
    timestamp=int(time.time()) if timestamp is None else timestamp
    def reset(document):
        check_clock(document,timestamp)
        actor=next((u for u in document['users'] if u['username']==operator and u['enabled'] and u['role']=='admin'),None)
        user=next((u for u in document['users'] if u['username']==username),None)
        if actor is None or user is None: raise ValueError('Invalid account or operator')
        user['password_hash']=password_hash
        if enable: user['enabled']=True
        document['sessions'][:]=[s for s in document['sessions'] if s['user_id']!=user['id']]
        record_event(document,actor['id'],'recovery',user['id'],'password_reset',timestamp=timestamp)
    store.transact(reset)


def read_password():
    password=getpass.getpass('New password: ')
    if password!=getpass.getpass('Confirm new password: ') or not valid_password(password):
        raise ValueError('Password policy or confirmation failed')
    return password


def main():
    parser=argparse.ArgumentParser(description='Offline recovery; keep API stopped and preserve the old volume.')
    commands=parser.add_subparsers(dest='command',required=True)
    restore=commands.add_parser('restore')
    restore.add_argument('--snapshot',required=True)
    restore.add_argument('--target',required=True)
    restore.add_argument('--sha256',required=True)
    reset=commands.add_parser('reset-password')
    reset.add_argument('--store',required=True)
    reset.add_argument('--operator',required=True)
    reset.add_argument('--username',required=True)
    reset.add_argument('--enable',action='store_true')
    args=parser.parse_args()
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        parser.error('An interactive terminal is required; credentials cannot be piped.')
    try:
        if args.command=='restore':
            if Path(args.target).exists(): raise FileExistsError()
            verified_snapshot(args.snapshot,args.sha256)
            username=input('New recovery administrator username: ')
            display_name=input('Display name: ')
            report=recover(args.snapshot,args.target,args.sha256,username,display_name,read_password())
            print(json.dumps(report,sort_keys=True))
        else:
            reset_password(V2Store(args.store),args.operator,args.username,read_password(),enable=args.enable)
            print('Password reset; existing sessions revoked. Account enabled only if explicitly requested.')
    except (EOFError,KeyboardInterrupt):
        parser.exit(1,'Recovery operation cancelled.\n')
    except (ValueError,OSError,portalocker.exceptions.LockException):
        parser.exit(1,'Recovery operation failed. Inspect the archive, target and clock before retrying.\n')


if __name__=='__main__': main()
