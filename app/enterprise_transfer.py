"""Catalogue transfers: JSON on the wire, atomic preparation before commit."""
import json
import uuid

from flask import Blueprint, request

from .audit import record_event
from .catalogue_transfer import ImportConflict, export_csv, export_document, normalize, parse_csv, prepare_import
from .enterprise_auth import error, no_cache, payload
from .enterprise_catalogue import management
from .idempotency import run_once
from .validation import ValidationError

bp=Blueprint('enterprise_transfer',__name__,url_prefix='/api/v1/catalogue')
bp.after_request(no_cache)


@bp.before_request
def transfer_limit():
    if request.endpoint=='enterprise_transfer.import_catalogue':
        # A JSON carrier can expand a one-MiB UTF-8 file with escapes.
        request.max_content_length=8*1024*1024


@bp.post('/import')
def import_catalogue():
    def transfer(document,user):
        data=payload(('format','content'),('duplicate_policy',))
        operation_id=str(uuid.uuid4())
        policy=data.get('duplicate_policy','reject')
        try:
            if data['format'] not in ('json','csv') or policy not in ('reject','skip','append_copies'):
                raise ValidationError({'file':'Format ou politique invalide.'})
            try:
                text=data['content'] if isinstance(data['content'],str) else json.dumps(data['content'],ensure_ascii=False,separators=(',',':'))
                if len(text.encode('utf-8'))>1024*1024:
                    raise ValueError()
            except (ValueError,UnicodeError,RecursionError):
                raise ValidationError({'file':'Fichier UTF-8 requis, de 1 Mio maximum.'}) from None
            incoming=normalize(parse_csv(data['content']) if data['format']=='csv' else data['content'])
        except ValidationError as exc:
            record_event(document,user['id'],'import',operation_id,'failed')
            return error(422,'validation_error','Catalogue invalide.',exc.fields)
        def commit():
            try:
                candidate,counts=prepare_import(document,incoming,policy)
            except ImportConflict:
                record_event(document,user['id'],'import',operation_id,'failed')
                return error(409,'import_conflict','Conflit dans le lot ; aucun film ou exemplaire importé.')
            document['movies']=candidate['movies']
            document['copies']=candidate['copies']
            record_event(document,user['id'],'import',operation_id,'completed')
            return {'operation_id':operation_id,**counts}
        return run_once(document,user,{'format':data['format'],'content':data['content'],'duplicate_policy':policy},commit,'catalog.import',200)
    return management(transfer)


@bp.get('/export')
def export_catalogue():
    def transfer(document,user):
        if set(request.args)-{'format'} or len(request.args.getlist('format'))>1 or request.args.get('format','json') not in ('json','csv'):
            raise ValidationError({'format':'Format invalide.'})
        format=request.args.get('format','json')
        content=export_document(document)
        record_event(document,user['id'],'export',str(uuid.uuid4()),'completed')
        return {'format':format,'content':export_csv(content) if format=='csv' else content,'filename':'cinevault-catalogue.'+format}
    return management(transfer)
