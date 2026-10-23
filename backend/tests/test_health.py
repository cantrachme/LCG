
from fastapi.testclient import TestClient
from app.main import app


def test_health_check():
    client = TestClient(app)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "lcg-backend",
    }


def test_spa_preserves_api_and_asset_errors(tmp_path):
    from fastapi import FastAPI
    from app.main import SPAFiles
    (tmp_path / 'index.html').write_text('<html>LCG synthetic</html>')
    web = FastAPI()
    web.mount('/', SPAFiles(directory=tmp_path, html=True))
    client = TestClient(web)
    assert client.get('/').status_code == 200
    assert 'LCG synthetic' in client.get('/chat/synthetic').text
    assert client.get('/api/unknown').status_code == 404
    assert client.get('/missing.js').status_code == 404


def test_docker_peer_still_requires_host_origin_and_header(monkeypatch):
    from app.browser import local_request
    from fastapi import FastAPI, Depends
    web = FastAPI()
    @web.get('/check', dependencies=[Depends(local_request)])
    def check():
        return {'ok': True}
    client = TestClient(web, client=('172.20.0.1', 1234), base_url='http://localhost:8000')
    headers = {'X-LCG-Local': '1', 'Origin': 'http://localhost:8000'}
    monkeypatch.delenv('LCG_CONTAINER', raising=False)
    assert client.get('/check', headers=headers).status_code == 403
    monkeypatch.setenv('LCG_CONTAINER', '1')
    assert client.get('/check', headers=headers).status_code == 200
    assert client.get('/check', headers={'X-LCG-Local': '1'}).status_code == 403
    assert client.get('/check', headers={'X-LCG-Local': '1', 'Sec-Fetch-Site': 'same-origin'}).status_code == 200
    assert client.get('/check', headers={'Origin': headers['Origin']}).status_code == 403
    assert client.get('/check', headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/check', headers={**headers, 'Host': 'evil.example'}).status_code == 403
