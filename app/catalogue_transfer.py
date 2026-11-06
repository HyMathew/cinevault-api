"""Bounded catalogue-only formats, independent of private account data."""
import copy
import csv
import io
import json
import uuid

from .enterprise_catalogue import COPY_FIELDS,MOVIE_FIELDS,copy_data,movie_data
from .repository import unique_pairs
from .validation import ValidationError

COLUMNS=('movie_id','copy_id','title','year','director','actors','genres','synopsis','support','location','condition')
MAX_MOVIES=1000
MAX_COPIES=5000


class ImportConflict(Exception):
    pass


def invalid():
    raise ValidationError({'file':'Catalogue invalide ou limites dépassées.'})


def identifier(value):
    try:
        if not isinstance(value,str) or str(uuid.UUID(value))!=value:
            invalid()
    except (ValueError,AttributeError):
        invalid()
    return value


def load_json(value):
    try:
        return json.loads(value,object_pairs_hook=unique_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError,RecursionError):
        invalid()


def normalize(content):
    if isinstance(content,str):
        content=load_json(content)
    if not isinstance(content,dict) or set(content)!={'format_version','movies'} or type(content['format_version']) is not int or content['format_version']!=1 or not isinstance(content['movies'],list) or len(content['movies'])>MAX_MOVIES:
        invalid()
    result=[]
    ids,keys=set(),set()
    count=0
    for item in content['movies']:
        if not isinstance(item,dict) or not set((*MOVIE_FIELDS,'copies')).issubset(item) or set(item)-set((*MOVIE_FIELDS,'copies','genres','id')) or not isinstance(item['copies'],list):
            invalid()
        movie=movie_data(item)
        if 'id' in item:
            movie['id']=identifier(item['id'])
            if movie['id'] in ids:
                invalid()
            ids.add(movie['id'])
        key=(movie['title'].casefold(),movie['year'],movie['director'].casefold())
        if key in keys:
            invalid()
        keys.add(key)
        copies=[]
        count+=len(item['copies'])
        if count>MAX_COPIES:
            invalid()
        for value in item['copies']:
            if not isinstance(value,dict) or set(value)-set((*COPY_FIELDS,'id')) or not set(COPY_FIELDS).issubset(value):
                invalid()
            entry=copy_data({k:value[k] for k in COPY_FIELDS})
            if 'id' in value:
                entry['id']=identifier(value['id'])
                if entry['id'] in ids:
                    invalid()
                ids.add(entry['id'])
            copies.append(entry)
        result.append({**movie,'copies':copies})
    return result


def parse_csv(content):
    if not isinstance(content,str) or '\x00' in content:
        invalid()
    try:
        reader=csv.reader(io.StringIO(content.removeprefix('\ufeff'),newline=''),strict=True)
        if tuple(next(reader))!=COLUMNS:
            invalid()
        grouped={}
        for index,values in enumerate(reader):
            if index>=MAX_MOVIES+MAX_COPIES or len(values)!=len(COLUMNS):
                invalid()
            row=dict(zip(COLUMNS,values))
            if not row['year'].isascii() or not row['year'].isdigit():
                invalid()
            metadata={'title':row['title'],'year':int(row['year']),'director':row['director'],
                'actors':load_json(row['actors']),'genres':load_json(row['genres']),'synopsis':row['synopsis']}
            key=('id',row['movie_id']) if row['movie_id'] else ('natural',row['title'].strip().casefold(),metadata['year'],row['director'].strip().casefold())
            if key not in grouped:
                grouped[key]={**metadata,'copies':[]}
                if row['movie_id']:
                    grouped[key]['id']=row['movie_id']
            elif any(grouped[key][k]!=v for k,v in metadata.items()):
                invalid()
            if any(row[k] for k in ('copy_id','support','location','condition')):
                entry={k:row[k] for k in COPY_FIELDS}
                if row['copy_id']:
                    entry['id']=row['copy_id']
                grouped[key]['copies'].append(entry)
        return {'format_version':1,'movies':list(grouped.values())}
    except (csv.Error,StopIteration,UnicodeError,ValueError,OverflowError):
        invalid()


def export_document(document):
    return {'format_version':1,'movies':[{**{k:movie[k] for k in ('id',*MOVIE_FIELDS,'genres')},'copies':[
        {k:entry[k] for k in ('id',*COPY_FIELDS)} for entry in document['copies'] if entry['movie_id']==movie['id']
    ]} for movie in document['movies']]}


def csv_safe(value):
    value=str(value)
    if value[:1] in ('\t','\r','\n') or value.lstrip(' \t\r\n')[:1] in ('=','+','-','@'):
        return "'"+value
    return value


def export_csv(content):
    output=io.StringIO(newline='')
    writer=csv.writer(output,quoting=csv.QUOTE_ALL)
    writer.writerow(COLUMNS)
    for movie in content['movies']:
        for entry in movie['copies'] or [None]:
            row={'movie_id':movie['id'],'copy_id':entry['id'] if entry else '',
                **{k:movie[k] for k in ('title','year','director','synopsis')},
                'actors':json.dumps(movie['actors'],ensure_ascii=False),'genres':json.dumps(movie['genres'],ensure_ascii=False),
                **{k:entry[k] if entry else '' for k in COPY_FIELDS}}
            writer.writerow([csv_safe(row[k]) for k in COLUMNS])
    return '\ufeff'+output.getvalue()


def prepare_import(document,incoming,policy):
    candidate=copy.deepcopy(document)
    used={entry['id'] for collection in candidate.values() if isinstance(collection,list) for entry in collection if isinstance(entry,dict) and 'id' in entry}
    by_key={(m['title'].casefold(),m['year'],m['director'].casefold()):m for m in candidate['movies']}
    result={'created_movies':0,'created_copies':0,'skipped_movies':0,'skipped_copies':0}
    for item in incoming:
        attrs={k:v for k,v in item.items() if k not in ('id','copies')}
        key=(item['title'].casefold(),item['year'],item['director'].casefold())
        existing=by_key.get(key)
        if item.get('id') in used and (existing is None or existing['id']!=item['id']):
            raise ImportConflict()
        if existing:
            if policy=='reject':
                raise ImportConflict()
            result['skipped_movies']+=1
            if policy=='skip':
                result['skipped_copies']+=len(item['copies'])
                continue
            if any(existing[k]!=v for k,v in attrs.items()):
                raise ImportConflict()
            movie_id=existing['id']
        else:
            movie_id=item.get('id') or str(uuid.uuid4())
            if movie_id in used:
                raise ImportConflict()
            candidate['movies'].append({'id':movie_id,**attrs})
            used.add(movie_id)
            result['created_movies']+=1
        for entry in item['copies']:
            copy_id=entry.get('id') or str(uuid.uuid4())
            copy_attrs={k:entry[k] for k in COPY_FIELDS}
            if copy_id in used:
                old=next((c for c in candidate['copies'] if c['id']==copy_id),None)
                if policy=='append_copies' and old and old['movie_id']==movie_id and all(old[k]==v for k,v in copy_attrs.items()):
                    result['skipped_copies']+=1
                    continue
                raise ImportConflict()
            candidate['copies'].append({'id':copy_id,'movie_id':movie_id,**copy_attrs})
            used.add(copy_id)
            result['created_copies']+=1
    return candidate,result
