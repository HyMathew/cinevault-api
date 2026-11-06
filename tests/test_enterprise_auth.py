import json
import pytest

from app import create_app
from app.storage_v2 import V2Store


@pytest.fixture
def enterprise(tmp_path):
    legacy = tmp_path / 'legacy.json'
    v2 = tmp_path / 'enterprise.json'
    V2Store(v2).initialize()
    app = create_app({'TESTING': True, 'DATA_FILE': str(legacy), 'ENTERPRISE_ENABLED': True,
                      'ENTERPRISE_DATA_FILE': str(v2), 'AUTH_THROTTLE_KEY': b'x' * 32})
    return app.test_client(), v2


def test_enterprise_is_opt_in_and_legacy_mutations_blocked(tmp_path, enterprise):
    client, path = enterprise
    assert client.post('/api/films', json={}).status_code == 403
    assert client.put('/api/films/some-id', json={}).status_code == 403
    assert client.delete('/api/films/some-id').status_code == 403
    assert client.get('/api/films').status_code == 200
    default_client = create_app({'TESTING': True, 'DATA_FILE': str(tmp_path / 'legacy2.json')}).test_client()
    assert default_client.post('/api/v1/auth/login', json={}).status_code == 404
    assert default_client.post('/api/films', json={}).status_code == 422


def test_registration_login_and_logout(enterprise):
    client, path = enterprise
    user = {'username':'alice', 'display_name':'Alice', 'password':'test passphrase 123'}
    response = client.post('/api/v1/auth/register', json=user)
    assert response.status_code == 201
    assert response.json['role'] == 'member'
    assert 'password_hash' not in response.json
    assert b'test passphrase 123' not in path.read_bytes()
    assert client.post('/api/v1/auth/register', json=user).status_code == 409
    assert client.get('/api/v1/me').status_code == 401
    login = client.post('/api/v1/auth/login', json={'username':'alice','password':user['password']})
    assert login.status_code == 200
    assert login.json['expires_in'] == 28800
    token = login.json['token']
    assert token.encode() not in path.read_bytes()
    auth = {'Authorization':'Bearer ' + token}
    assert client.get('/api/v1/me', headers=auth).json['username'] == 'alice'
    assert client.post('/api/v1/auth/logout', headers=auth).status_code == 204
    assert client.get('/api/v1/me', headers=auth).status_code == 401


def account(client, username='alice', role='member', path=None):
    password = 'test passphrase 123'
    profile = client.post('/api/v1/auth/register', json={
        'username': username, 'display_name': username.title(), 'password': password}).json
    if role != 'member':
        V2Store(path).transact(lambda doc: next(u for u in doc['users'] if u['id'] == profile['id']).update(role=role))
    token = client.post('/api/v1/auth/login', json={'username': username, 'password': password}).json['token']
    return profile, {'Authorization': 'Bearer ' + token}


def test_profile_cannot_escalate_or_read_others(enterprise):
    client, path = enterprise
    alice, auth = account(client)
    bob, _ = account(client, 'bobby')
    assert client.patch('/api/v1/me', headers=auth, json={'display_name':'Alice Updated'}).json['display_name'] == 'Alice Updated'
    for extra in ({'role':'admin'}, {'enabled':False}, {'username':'changed'}, {}):
        assert client.patch('/api/v1/me', headers=auth, json=extra).status_code == 422
    assert client.get('/api/v1/users/' + alice['id'], headers=auth).status_code == 200
    assert client.get('/api/v1/users/' + bob['id'], headers=auth).status_code == 403
    assert client.get('/api/v1/users', headers=auth).status_code == 403
    assert client.patch('/api/v1/users/' + alice['id'], headers=auth, json={'role':'admin'}).status_code == 403
    assert client.get('/api/v1/me', headers={'X-User-ID':alice['id'], 'X-Role':'admin'}).status_code == 401


def test_password_change_revokes_all_sessions(enterprise):
    client, path = enterprise
    _, auth = account(client)
    login = client.post('/api/v1/auth/login', json={'username':'alice','password':'test passphrase 123'})
    other = {'Authorization': 'Bearer ' + login.json['token']}
    endpoint = '/api/v1/me/password'
    assert client.put(endpoint, headers=auth, json={'current_password':'wrong', 'new_password':'new passphrase 456'}).status_code == 401
    assert client.put(endpoint, headers=auth, json={'current_password':'test passphrase 123', 'new_password':'short'}).status_code == 422
    assert client.put(endpoint, headers=auth, json={'current_password':'test passphrase 123', 'new_password':'new passphrase 456'}).status_code == 204
    for header in (auth, other):
        assert client.get('/api/v1/me', headers=header).status_code == 401
    assert client.post('/api/v1/auth/login', json={'username':'alice','password':'test passphrase 123'}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'username':'alice','password':'new passphrase 456'}).status_code == 200


def test_admin_permissions_and_last_admin_guard(enterprise):
    client, path = enterprise
    admin, auth = account(client, 'adminone', 'admin', path)
    member, member_auth = account(client, 'memberone')
    assert client.get('/api/v1/users', headers=auth).status_code == 200
    assert client.get('/api/v1/users?limit=1001', headers=auth).status_code == 422
    assert client.get('/api/v1/users/not-found', headers=auth).status_code == 404
    for change in ({'enabled':False}, {'role':'member'}):
        assert client.patch('/api/v1/users/' + admin['id'], headers=auth, json=change).status_code == 409
    target = '/api/v1/users/' + member['id']
    for change in ({'enabled':1}, {'role':'owner'}, {'password_hash':'injected'}, {}):
        assert client.patch(target, headers=auth, json=change).status_code == 422
    assert client.patch(target, headers=auth, json={'role':'manager'}).json['role'] == 'manager'
    assert client.get('/api/v1/me', headers=member_auth).status_code == 401
    manager_auth = {'Authorization':'Bearer ' + client.post('/api/v1/auth/login', json={'username':'memberone','password':'test passphrase 123'}).json['token']}
    assert client.get('/api/v1/users', headers=manager_auth).status_code == 403
    assert client.patch(target, headers=auth, json={'enabled':False}).status_code == 200
    assert client.get('/api/v1/me', headers=manager_auth).status_code == 401
    assert client.post('/api/v1/auth/login', json={'username':'memberone','password':'test passphrase 123'}).status_code == 401


def test_bootstrap_once_without_default_credentials(tmp_path):
    from app.bootstrap import bootstrap_admin
    path = tmp_path / 'bootstrap.json'
    store = V2Store(path)
    store.initialize()
    profile = bootstrap_admin(store, 'firstadmin', 'First Admin', 'unique bootstrap passphrase')
    assert profile['role'] == 'admin'
    assert 'password_hash' not in profile
    before = path.read_bytes()
    with pytest.raises(ValueError, match='administrator'):
        bootstrap_admin(store, 'secondadmin', 'Second', 'another bootstrap passphrase')
    assert path.read_bytes() == before


def test_expiry_throttle_and_no_secret_disclosure(enterprise):
    client, path = enterprise
    client.application.config['AUTH_CLOCK'] = lambda: 10000
    _, auth = account(client)
    client.application.config['AUTH_CLOCK'] = lambda: 11800
    assert client.get('/api/v1/me', headers=auth).status_code == 401
    for _ in range(5):
        assert client.post('/api/v1/auth/login', json={'username':'alice','password':'wrong'}).status_code == 401
    response = client.post('/api/v1/auth/login', json={'username':'alice','password':'test passphrase 123'})
    assert response.status_code == 429 and int(response.headers['Retry-After']) > 0
    client.application.config['AUTH_CLOCK'] = lambda: 12701
    response = client.post('/api/v1/auth/login', json={'username':'alice','password':'test passphrase 123'})
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    path.write_text('{broken', encoding='utf-8')
    assert client.get('/health').status_code == 503
    response = client.get('/api/v1/me', headers=auth)
    assert response.status_code == 503 and 'Traceback' not in response.text


@pytest.mark.parametrize('changes', [
    {'password':'short'}, {'password':'x' * 257}, {'password':'x' * 11},
    {'password':123}, {'username':'ab'}, {'username':'bad name'},
    {'display_name':' padded '}, {'display_name':''}, {'role':'admin'},
])
def test_invalid_registration_never_writes(enterprise, changes):
    client, path = enterprise
    before = path.read_bytes()
    body = {'username':'alice','display_name':'Alice','password':'test passphrase 123', **changes}
    assert client.post('/api/v1/auth/register', json=body).status_code == 422
    assert path.read_bytes() == before


def test_malformed_requests_and_read_only_health(enterprise):
    client, path = enterprise
    before = path.read_bytes()
    assert client.get('/health').status_code == 200
    assert path.read_bytes() == before
    endpoint = '/api/v1/auth/register'
    assert client.post(endpoint, data='{}', content_type='text/plain').status_code == 415
    for raw in ('{"username":"a","username":"b"}', '{', '{"bad":NaN}'):
        assert client.post(endpoint, data=raw, content_type='application/json').status_code == 400
    assert path.read_bytes() == before


def test_sessions_survive_api_restart_but_expire_absolutely(enterprise):
    client, path = enterprise
    client.application.config['AUTH_CLOCK'] = lambda: 10000
    _, auth = account(client)
    app = create_app({**client.application.config, 'AUTH_CLOCK': lambda: 10001})
    restarted = app.test_client()
    assert restarted.get('/api/v1/me', headers=auth).status_code == 200
    # Keep the session active until the absolute eight-hour boundary.
    for tick in range(11000, 38800, 1000):
        app.config['AUTH_CLOCK'] = lambda tick=tick: tick
        assert restarted.get('/api/v1/me', headers=auth).status_code == 200
    app.config['AUTH_CLOCK'] = lambda: 38800
    assert restarted.get('/api/v1/me', headers=auth).status_code == 401


def test_concurrent_admin_demotions_keep_one_admin(enterprise):
    from concurrent.futures import ThreadPoolExecutor
    client, path = enterprise
    first, first_auth = account(client, 'firstadmin', 'admin', path)
    second, second_auth = account(client, 'secondadmin', 'admin', path)
    def demote(pair):
        profile, auth = pair
        with client.application.test_client() as other:
            return other.patch('/api/v1/users/' + profile['id'], headers=auth, json={'role':'member'}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(demote, [(first, first_auth), (second, second_auth)]))
    assert sorted(outcomes) == [200, 409]
    assert len([u for u in V2Store(path).read()['users'] if u['role'] == 'admin' and u['enabled']]) == 1


def test_auth_document_and_responses_match_json_schemas(enterprise):
    from pathlib import Path
    from jsonschema import Draft202012Validator
    client, path = enterprise
    profile, auth = account(client)
    client.post('/api/v1/auth/login', json={'username':'alice','password':'wrong'})
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / 'schemas/catalogue-v2.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(V2Store(path).read())
    spec = json.loads((root / 'docs/openapi-v1.json').read_text(encoding='utf-8'))
    public_schema = {**spec['components']['schemas']['Profile'], '$defs':spec['components']['schemas']}
    Draft202012Validator(public_schema).validate(client.get('/api/v1/me', headers=auth).json)
    assert set(profile) == {'id','username','display_name','role','enabled'}


def test_invalid_sessions_never_rewrite_the_document(enterprise):
    client,path = enterprise
    _,auth = account(client)
    client.post('/api/v1/auth/logout',headers=auth)
    before = path.read_bytes()
    for headers in ({},auth,{'Authorization':'Bearer '+'f' * 43}):
        assert client.get('/api/v1/me',headers=headers).status_code == 401
        assert path.read_bytes() == before
