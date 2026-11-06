from test_enterprise_auth import enterprise,account
from test_enterprise_catalogue import MOVIE,COPY


def context(enterprise):
    client,path=enterprise
    client.application.config['AUTH_CLOCK']=lambda:1700000000
    _,manager=account(client,'insightmanager','manager',path)
    _,member=account(client,'insightmember')
    _,other=account(client,'insightother')
    first=client.post('/api/v1/movies',headers=manager,json={**MOVIE,'title':'Alpha','genres':['Drame']}).json
    second=client.post('/api/v1/movies',headers=manager,json={**MOVIE,'title':'Beta','genres':['Drame']}).json
    third=client.post('/api/v1/movies',headers=manager,json={**MOVIE,'title':'Gamma','genres':['Comédie'],'director':'Other','actors':[]}).json
    copies=[client.post('/api/v1/copies',headers=manager,json={'movie_id':m['id'],**COPY}).json for m in (first,second)]
    client.post('/api/v1/copies',headers=manager,json={'movie_id':second['id'],**COPY,'condition':'damaged'})
    return client,path,manager,member,other,first,second,third,copies


def test_statistics_exact_inventory_personal_scope_and_permissions(enterprise):
    client,path,manager,member,other,first,second,third,copies=context(enterprise)
    assert client.get('/api/v1/statistics').status_code==401
    for kind in ('favorite','watched'):
        assert client.post('/api/v1/collections',headers=member,json={'movie_id':first['id'],'kind':kind}).status_code==201
    client.post('/api/v1/collections',headers=other,json={'movie_id':second['id'],'kind':'favorite'})
    loan=client.post('/api/v1/loans',headers=member,json={'movie_id':first['id']}).json
    assert client.post('/api/v1/loans/'+loan['id']+'/approve',headers={**manager,'If-Match':loan['_etag']},json={'copy_id':copies[0]['id'],'due_at':1700000010}).status_code==200
    client.application.config['AUTH_CLOCK']=lambda:1700000020
    response=client.get('/api/v1/statistics',headers=member)
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    result=response.json
    assert result['catalogue']['movies']==3 and result['catalogue']['copies']==3
    assert result['catalogue']['available']==1 and result['catalogue']['damaged']==1
    assert result['catalogue']['by_genre']=={'Drame':2,'Comédie':1}
    assert result['personal']['collection_entries']==2 and result['personal']['collection_movies']==1
    assert result['personal']['by_kind']['favorite']==1 and result['personal']['overdue']==1
    assert 'operations' not in result
    assert client.get('/api/v1/statistics?scope=all',headers=member).status_code==403
    global_=client.get('/api/v1/statistics?scope=all',headers=manager).json
    assert global_['operations']['loans']['active']==1 and global_['operations']['overdue']==1
    assert global_['personal']['collection_entries']==0
    for params in ('owner_id=other','scope=bad','scope=mine&scope=all'):
        assert client.get('/api/v1/statistics?'+params,headers=manager).status_code==422


def test_recommendation_uses_own_signals_explains_score_and_preferences(enterprise):
    client,path,manager,member,other,first,second,third,copies=context(enterprise)
    client.post('/api/v1/collections',headers=member,json={'movie_id':first['id'],'kind':'favorite'})
    client.post('/api/v1/collections',headers=other,json={'movie_id':third['id'],'kind':'favorite'})
    response=client.get('/api/v1/recommendations',headers=member)
    assert response.status_code==200
    result=response.json
    assert result['personalized'] is True
    assert result['recommendations'][0]['movie']['id']==second['id']
    assert all(r['movie']['id']!=first['id'] for r in result['recommendations'])
    for item in result['recommendations']:
        assert item['score']==sum(reason['points'] for reason in item['reasons'])
        assert all(set(reason)=={'code','label','points'} for reason in item['reasons'])
    before=client.get('/api/v1/recommendations',headers=member).json
    client.post('/api/v1/collections',headers=other,json={'movie_id':third['id'],'kind':'watched'})
    assert client.get('/api/v1/recommendations',headers=member).json==before
    preferred=client.get('/api/v1/recommendations?genre=Comédie',headers=other).json
    assert preferred['personalized']
    assert not any(r['code']=='preferred_genre' for item in preferred['recommendations'] for r in item['reasons']) # favorite Gamma is excluded
    cold=client.get('/api/v1/recommendations',headers=manager).json
    assert cold['personalized'] is False and all(r['score']==0 for r in cold['recommendations'])
    selected=client.get('/api/v1/recommendations?genre=Comédie&limit=1',headers=manager).json
    assert selected['recommendations'][0]['movie']['id']==third['id']
    assert selected['recommendations'][0]['reasons'][0]['code']=='preferred_genre'
    for params in ('owner_id=other','limit=25','genre='+('x'*81),'&'.join(['genre=Drame']*11)):
        assert client.get('/api/v1/recommendations?'+params,headers=member).status_code==422


def test_empty_catalogue_statistics_and_recommendations(enterprise):
    client,path=enterprise
    _,auth=account(client,'emptyinsights')
    statistics=client.get('/api/v1/statistics',headers=auth).json
    assert statistics['catalogue']['movies']==statistics['catalogue']['copies']==statistics['catalogue']['available']==0
    assert statistics['personal']['collection_entries']==statistics['personal']['overdue']==0
    result=client.get('/api/v1/recommendations',headers=auth).json
    assert result['recommendations']==[] and result['personalized'] is False


def test_queue_hold_expiration_and_returned_history_are_derived(enterprise):
    from app.storage_v2 import V2Store
    from app.insights import statistics,recommendations
    client,path,manager,member,other,first,second,third,copies=context(enterprise)
    loan=client.post('/api/v1/loans',headers=member,json={'movie_id':first['id']}).json
    active=client.post('/api/v1/loans/'+loan['id']+'/approve',headers={**manager,'If-Match':loan['_etag']},json={'copy_id':copies[0]['id'],'due_at':1700000010}).json
    reservation=client.post('/api/v1/reservations',headers=other,json={'movie_id':first['id']})
    assert reservation.status_code==201
    assert client.post('/api/v1/loans/'+loan['id']+'/return',headers={**manager,'If-Match':active['_etag']}).status_code==200
    doc=V2Store(path).read(); user_id=doc['loans'][0]['borrower_id']
    result=statistics(doc,user_id,1700000000)
    assert result['catalogue']['free']==2 and result['catalogue']['held_for_queue']==1 and result['catalogue']['available']==1
    expired=statistics(doc,user_id,reservation.json['expires_at'])
    assert expired['catalogue']['held_for_queue']==0 and expired['catalogue']['available']==2
    ranked=recommendations(doc,user_id,1700000000)['recommendations']
    assert ranked[0]['movie']['id']==second['id']
    assert any(reason['code']=='similar_genre' and reason['points']==1 for reason in ranked[0]['reasons'])
    assert first['id'] not in [item['movie']['id'] for item in ranked]


def test_collection_changes_update_scores_and_owned_support_counts(enterprise):
    client,path,manager,member,other,first,second,third,copies=context(enterprise)
    favorite=client.post('/api/v1/collections',headers=member,json={'movie_id':first['id'],'kind':'favorite'}).json
    client.post('/api/v1/collections',headers=member,json={'movie_id':third['id'],'kind':'owned','supports':['dvd','blu-ray']})
    result=client.get('/api/v1/recommendations',headers=member).json
    assert len(result['recommendations'])==1 and result['recommendations'][0]['movie']['id']==second['id']
    assert any(reason['points']==3 and reason['code']=='similar_genre' for reason in result['recommendations'][0]['reasons'])
    assert client.delete('/api/v1/collections/'+favorite['id'],headers={**member,'If-Match':favorite['_etag']}).status_code==204
    result=client.get('/api/v1/recommendations',headers=member).json
    assert result['personalized'] is False and all(item['score']==0 for item in result['recommendations'])
    stats=client.get('/api/v1/statistics',headers=member).json
    assert stats['personal']['by_owned_support']['dvd']==stats['personal']['by_owned_support']['blu-ray']==1
    assert stats['personal']['collection_movies']==1


def test_insights_responses_match_documented_schemas(enterprise):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    client,path,manager,member,other,first,second,third,copies=context(enterprise)
    api=json.loads((Path(__file__).resolve().parents[1]/'docs/openapi-v1.json').read_text(encoding='utf-8'))
    for route,schema,headers in (('/statistics','Statistics',member),('/statistics?scope=all','Statistics',manager),('/recommendations','Recommendations',member)):
        Draft202012Validator({'$ref':'#/components/schemas/'+schema,'components':api['components']}).validate(client.get('/api/v1'+route,headers=headers).json)
