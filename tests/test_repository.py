import json
import subprocess
import sys

from app import create_app


def test_concurrent_processes_preserve_all_writes(tmp_path):
    path = tmp_path / 'films.json'
    code = '''
import sys
from app import create_app
c = create_app({'DATA_FILE': sys.argv[1]}).test_client()
for n in range(8):
    r = c.post('/api/films', json={'titre': sys.argv[2] + str(n), 'annee': 2001, 'realisateur': 'R', 'acteurs': [], 'support': 'dvd'})
    assert r.status_code == 201, r.data
'''
    processes = [subprocess.Popen([sys.executable, '-c', code, str(path), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    for process in processes:
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, (out, err)
    films = json.loads(path.read_text(encoding='utf-8'))['films']
    assert len(films) == 32
    assert len({film['id'] for film in films}) == 32


def test_failed_replace_preserves_old_document(tmp_path, monkeypatch):
    path = tmp_path / 'films.json'
    client = create_app({'TESTING': True, 'DATA_FILE': str(path)}).test_client()
    assert client.get('/health').status_code == 200
    old = path.read_bytes()
    def fail(*args):
        raise OSError('simulated failure')
    monkeypatch.setattr('app.repository.os.replace', fail)
    response = client.post('/api/films', json={'titre':'T', 'annee':2001, 'realisateur':'R', 'acteurs':[], 'support':'dvd'})
    assert response.status_code == 503
    assert path.read_bytes() == old
    assert not list(tmp_path.glob('.films-*.tmp'))
