import json
from pathlib import Path

import pytest


def entity(qid='Q151599',film=True):
    def claim(value): return {'rank':'normal','mainsnak':{'snaktype':'value','datavalue':{'value':value}}}
    return {'id':qid,'lastrevid':123,'labels':{'fr':{'value':'Métropolis'}},'descriptions':{'fr':{'value':'Film de science-fiction'}},
        'claims':{'P31':[claim({'id':'Q11424' if film else 'Q5'})],
            'P57':[claim({'id':'Q19504'})],'P161':[claim({'id':'Q63781'})],'P136':[claim({'id':'Q471839'})],
            'P577':[claim({'time':'+1927-01-10T00:00:00Z','precision':11})],
            'P3383':[claim('Example poster.jpg')],'P18':[claim('Unrelated still.jpg')]}}


def test_film_parser_resolves_fields_and_rejects_homonyms():
    from app.metadata import normalize_film,MetadataUnavailable
    labels={'Q19504':'Fritz Lang','Q63781':'Brigitte Helm','Q471839':'Science-fiction'}
    result=normalize_film(entity(),labels,1700000000)
    assert result['title']=='Métropolis' and result['year']==1927 and result['director']=='Fritz Lang'
    assert result['actors']==['Brigitte Helm'] and result['genres']==['Science-fiction']
    assert result['description']=='Film de science-fiction' and result['poster_filename']=='Example poster.jpg'
    assert result['revision']==123 and result['retrieved_at']==1700000000
    with pytest.raises(MetadataUnavailable): normalize_film(entity(film=False),labels,1700000000)
    partial=entity(); partial['claims'].pop('P57'); partial['claims'].pop('P577')
    result=normalize_film(partial,labels,1700000000)
    assert result['director']=='' and result['year'] is None and result['warnings']


def test_poster_licence_and_url_are_checked_and_credit_is_plain_text():
    from app.metadata import normalize_poster
    info={'thumburl':'https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Example.jpg/480px-Example.jpg',
        'thumbmime':'image/jpeg','extmetadata':{key:{'value':value} for key,value in {
            'LicenseShortName':'CC BY-SA 4.0','LicenseUrl':'https://creativecommons.org/licenses/by-sa/4.0/',
            'Artist':'<b>Artist</b><script>bad()</script>','AttributionRequired':'true','Restrictions':''}.items()}}
    result=normalize_poster('Example.jpg',info)
    assert result['artist']=='Artist' and result['license']=='CC BY-SA 4.0'
    assert result['source_url'].startswith('https://commons.wikimedia.org/wiki/File:')
    modern={**info,'thumburl':info['thumburl'].replace('upload.wikimedia.org','thumb.wikimedia.org')+'?utm_source=commons.wikimedia.org&utm_campaign=imageinfo'}
    assert normalize_poster('Example.jpg',modern)['image_url']==modern['thumburl'].split('?',1)[0]
    assert normalize_poster('Example.jpg',{**modern,'thumburl':modern['thumburl']+'&url=http://127.0.0.1'}) is None
    for change in ({'thumburl':'http://127.0.0.1/private'}, {'thumburl':'https://upload.wikimedia.org.evil.test/x.jpg'},
                   {'thumburl':'https://upload.wikimedia.org@evil.test/x.jpg'}, {'thumbmime':'image/svg+xml'}):
        assert normalize_poster('Example.jpg',{**info,**change}) is None
    for field,value in (('LicenseShortName','Non-free'),('Restrictions','trademark'),('Artist','')):
        changed={**info,'extmetadata':{**info['extmetadata'],field:{'value':value}}}
        assert normalize_poster('Example.jpg',changed) is None


def test_shared_cache_fresh_hit_stale_fallback_and_quota(tmp_path):
    from app.metadata import MetadataCache,MetadataUnavailable,MetadataBusy
    now=[1700000000]; calls=[]
    first=MetadataCache(tmp_path/'cache.json',clock=lambda:now[0])
    second=MetadataCache(tmp_path/'cache.json',clock=lambda:now[0])
    def fetch(request):
        calls.append('network'); request(); return {'items':['public film']}
    assert first.fetch('search:q',fetch)['cache']['status']=='fresh'
    assert second.fetch('search:q',fetch)['items']==['public film'] and len(calls)==1
    now[0]+=86401
    def unavailable(request):
        request(); raise MetadataUnavailable()
    assert second.fetch('search:q',unavailable)['cache']['status']=='stale'
    now[0]+=8*86400
    with pytest.raises(MetadataUnavailable): second.fetch('search:q',unavailable)
    now[0]+=61
    for i in range(20): first.fetch('different:'+str(i),fetch)
    with pytest.raises(MetadataBusy): second.fetch('over-quota',fetch)


def test_cached_error_does_not_replace_good_entry_and_429_is_shared(tmp_path):
    from app.metadata import MetadataCache,MetadataBusy
    clock=[1700000000]
    first=MetadataCache(tmp_path/'cache.json',clock=lambda:clock[0])
    second=MetadataCache(tmp_path/'cache.json',clock=lambda:clock[0])
    def rate_limited(request):
        request(); raise MetadataBusy(120)
    with pytest.raises(MetadataBusy): first.fetch('blocked',rate_limited)
    calls=[]
    def forbidden(request): calls.append('must not run'); return {'items':[]}
    with pytest.raises(MetadataBusy): second.fetch('another',forbidden)
    assert calls==[]
    clock[0]+=121
    assert second.fetch('another',lambda request:{'items':[]})['items']==[]
    assert json.loads((tmp_path/'cache.json').read_text(encoding='utf-8'))['blocked_until']<=clock[0]


def test_corrupt_cache_and_backwards_clock_do_not_overwrite_state(tmp_path):
    from app.metadata import MetadataCache,MetadataUnavailable
    path=tmp_path/'cache.json'; path.write_bytes(b'{"bad":true}')
    cache=MetadataCache(path,clock=lambda:1700000000)
    with pytest.raises(MetadataUnavailable): cache.fetch('test',lambda reserve:{'items':[]})
    assert path.read_bytes()==b'{"bad":true}'
    path.unlink()
    clock=[1700000000]; cache=MetadataCache(path,clock=lambda:clock[0])
    cache.fetch('test',lambda reserve:{'items':[]}); original=path.read_bytes()
    clock[0]-=1
    with pytest.raises(MetadataUnavailable): cache.fetch('another',lambda reserve:{'items':[]})
    assert path.read_bytes()==original


def cache_in_process(path,queue):
    from app.metadata import MetadataCache
    cache=MetadataCache(path,clock=lambda:1700000000)
    def fetch(reserve):
        reserve(); return {'items':['one network fetch']}
    result=cache.fetch('same-query',fetch)
    queue.put(result['cache']['hit'])


def test_two_processes_share_one_fetch_and_cache_receipt(tmp_path):
    import multiprocessing
    path=tmp_path/'cache.json'; context=multiprocessing.get_context('spawn'); queue=context.Queue()
    processes=[context.Process(target=cache_in_process,args=(path,queue)) for _ in range(2)]
    for process in processes: process.start()
    for process in processes:
        process.join(20)
        if process.is_alive():
            process.terminate(); process.join()
            raise AssertionError('Cache process timed out')
        assert process.exitcode==0
    assert sorted(queue.get(timeout=5) for _ in processes)==[False,True]
    state=json.loads(path.read_text(encoding='utf-8'))
    assert state['count']==1 and len(state['entries'])==1
