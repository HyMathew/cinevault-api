import io
import json
from unittest.mock import Mock

import pytest

from test_metadata import entity


def test_wikimedia_detail_uses_only_fixed_hosts_and_caches(tmp_path,monkeypatch):
    from app.wikimedia import WikimediaClient
    calls=[]
    def remote(url,params,agent):
        calls.append((url,params,agent))
        if params.get('ids')=='Q151599': return {'entities':{'Q151599':entity()}}
        if params.get('action')=='wbgetentities':
            return {'entities':{qid:{'labels':{'fr':{'value':name}}} for qid,name in [('Q19504','Fritz Lang'),('Q63781','Brigitte Helm'),('Q471839','Science-fiction')]}}
        if params.get('prop')=='imageinfo': return {'query':{'pages':{'1':{'imageinfo':[]}}}}
        if params.get('list')=='search': return {'query':{'search':[{'title':'Q151599'}]}}
        raise AssertionError('Unexpected external request')
    monkeypatch.setattr('app.wikimedia.request_json',remote)
    client=WikimediaClient(tmp_path/'cache.json','CineVault/0.1 (https://example.test/project)',clock=lambda:1700000000)
    first=client.detail('Q151599')
    assert first['film']['director']=='Fritz Lang' and first['film']['poster'] is None
    assert first['cache']['hit'] is False
    assert client.detail('Q151599')['cache']['hit'] is True and len(calls)==3
    assert all(url in ('https://www.wikidata.org/w/api.php','https://commons.wikimedia.org/w/api.php') for url,params,agent in calls)
    assert all(params['maxlag']==5 for url,params,agent in calls)
    for identity in ('http://localhost','Q1/../../','not-a-qid'):
        with pytest.raises(ValueError): client.detail(identity)


def test_transport_rejects_oversize_content_and_sets_timeout(monkeypatch):
    from app.wikimedia import request_json
    from app.metadata import MetadataUnavailable
    response=Mock()
    response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
    response.headers={'Content-Type':'application/json'}
    response.read.return_value=b' '*1048577
    opener=Mock(); opener.open.return_value=response
    monkeypatch.setattr('urllib.request.build_opener',lambda *handlers:opener)
    with pytest.raises(MetadataUnavailable): request_json('https://www.wikidata.org/w/api.php',{'format':'json'},'CineVault/0.1 (https://example.test/project)')
    assert opener.open.call_args.kwargs['timeout']==4
    response.read.return_value=b'{"ok":1,"ok":2}'
    with pytest.raises(MetadataUnavailable): request_json('https://www.wikidata.org/w/api.php',{},'test agent')


def test_transport_429_and_maxlag_create_bounded_backoff(monkeypatch):
    from urllib.error import HTTPError
    from app.wikimedia import request_json
    from app.metadata import MetadataBusy
    opener=Mock(); opener.open.side_effect=HTTPError('https://www.wikidata.org',429,'too many',{'Retry-After':'120'},io.BytesIO())
    monkeypatch.setattr('urllib.request.build_opener',lambda *handlers:opener)
    with pytest.raises(MetadataBusy) as error: request_json('https://www.wikidata.org/w/api.php',{},'agent')
    assert error.value.retry_after==120
    response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
    response.headers={'Content-Type':'application/json'}; response.read.return_value=json.dumps({'error':{'code':'maxlag'}}).encode()
    opener.open.side_effect=None; opener.open.return_value=response
    with pytest.raises(MetadataBusy): request_json('https://www.wikidata.org/w/api.php',{},'agent')
