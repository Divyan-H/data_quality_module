"""Access control and the exposed-endpoint limits: token or session cookie on every API path, safe defaults when no
token is set, refusal to listen on a network address without one, bounded precheck bodies, callback allow-list."""
import pytest
from fastapi.testclient import TestClient

from src.api import api_server
from src.api.api_server import create_app
from src.orchestration.settings import load_service_settings
from src.security import auth

TOKEN = "s3cret-token-0123456789"


@pytest.fixture
def secured(tmp_path):
    app = create_app(str(tmp_path / "storage"), load_service_settings(workspace=tmp_path / "runs", auth_token=TOKEN))
    with TestClient(app) as c:
        yield c
        app.state.pipeline.shutdown()


def test_every_api_path_needs_the_token(secured):
    for method, path in (("get", "/api/v1/runs"), ("get", "/api/v1/options"), ("post", "/jobs"), ("get", "/openapi.json"),
                         ("post", "/review/precheck"), ("get", "/api/v1/runs/abc/files/x")):
        r = getattr(secured, method)(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "auth_required", path
    assert secured.get("/health").status_code == 200 and secured.get("/api/v1/health").status_code == 200


def test_bearer_token_works_and_a_wrong_one_does_not(secured):
    assert secured.get("/api/v1/runs", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert secured.get("/api/v1/runs", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_browser_session_cookie_sign_in_and_out(secured):
    assert secured.get("/api/v1/session").json() == {"auth_required": True, "signed_in": False}
    bad = secured.post("/api/v1/session", json={"token": "wrong"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "bad_token"
    ok = secured.post("/api/v1/session", json={"token": TOKEN})
    assert ok.status_code == 200 and auth.COOKIE in ok.cookies
    assert TOKEN not in ok.headers["set-cookie"]                        # the cookie carries an HMAC, not the token
    assert "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
    assert secured.get("/api/v1/runs").status_code == 200
    secured.delete("/api/v1/session")
    secured.cookies.clear()
    assert secured.get("/api/v1/runs").status_code == 401


def test_without_a_token_nothing_changes(client):
    assert client.get("/api/v1/session").json() == {"auth_required": False, "signed_in": True}
    assert client.get("/api/v1/runs").status_code == 200


def test_a_short_token_is_refused():
    with pytest.raises(auth.TokenError):
        auth.check_token("short")


def test_the_server_will_not_listen_on_a_network_address_without_a_token(monkeypatch):
    monkeypatch.delenv("DQ_API_TOKEN", raising=False)
    monkeypatch.delenv("DQ_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit, match="without an access token"):
        api_server.start_api_server({"host": "0.0.0.0", "port": 0})
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, **options: started.append((host, options["proxy_headers"])))
    monkeypatch.setenv("DQ_API_TOKEN", TOKEN)
    api_server.start_api_server({"host": "0.0.0.0", "port": 0})
    assert started == [("0.0.0.0", True)]


def test_job_callbacks_only_go_to_allowed_hosts(monkeypatch, tmp_path):
    assert api_server.callback_allowed("https://gateway.internal/cb")
    assert not api_server.callback_allowed("file:///etc/passwd")
    monkeypatch.setenv("STRUCTURE_CALLBACK_HOSTS", "gateway.internal")
    assert api_server.callback_allowed("https://gateway.internal/cb") and not api_server.callback_allowed("http://169.254.169.254/")
    with TestClient(create_app(str(tmp_path))) as c:
        r = c.post("/jobs", json={"job_id": "j1", "job_type": "data.structure", "output_prefix": "out",
                                  "callback_url": "http://169.254.169.254/latest", "job_token": "t"})
        assert r.status_code == 400 and "callback_url" in r.json()["error"]["message"]


def test_precheck_refuses_an_oversized_body(monkeypatch, tmp_path):
    monkeypatch.setattr(api_server, "PRECHECK_MAX_MB", 1)
    with TestClient(create_app(str(tmp_path))) as c:
        r = c.post("/review/precheck", content=b"x" * (1024 * 1024 + 10))
        assert r.status_code == 413
