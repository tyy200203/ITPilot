import asyncio
import json
from collections import deque

import httpx
import pytest

from api import main
from api.auth import get_auth_config
from agents.agent_orchestrator import AgentOrchestrator, AgentType, OrchestratorResult


USER_1_TOKEN = "user-one-secret-token-0123456789"
USER_2_TOKEN = "user-two-secret-token-0123456789"
ADMIN_TOKEN = "admin-secret-token-012345678901"


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class ApiClient:
    def request(self, method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ITPILOT_USER_TOKENS", json.dumps({
        "u1": USER_1_TOKEN,
        "u2": USER_2_TOKEN,
    }))
    monkeypatch.setenv("ITPILOT_ADMIN_TOKEN", ADMIN_TOKEN)
    get_auth_config.cache_clear()
    yield ApiClient()  # No lifespan: auth checks run before service components.
    get_auth_config.cache_clear()


def test_auth_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("ITPILOT_USER_TOKENS", raising=False)
    monkeypatch.delenv("ITPILOT_ADMIN_TOKEN", raising=False)
    get_auth_config.cache_clear()
    with pytest.raises(RuntimeError):
        get_auth_config()

    monkeypatch.setenv("ITPILOT_USER_TOKENS", json.dumps({"u1": USER_1_TOKEN}))
    monkeypatch.setenv("ITPILOT_ADMIN_TOKEN", USER_1_TOKEN)
    get_auth_config.cache_clear()
    with pytest.raises(RuntimeError, match="distinct"):
        get_auth_config()
    get_auth_config.cache_clear()


@pytest.mark.parametrize("path,method,kwargs", [
    ("/skills/reload", "post", {}),
    ("/knowledge/add", "post", {"json": {"documents": []}}),
    ("/knowledge/upload", "post", {"files": {"file": ("note.txt", b"hello")}}),
    ("/eval/run", "post", {}),
    ("/trace/tools", "get", {}),
    ("/monitor", "get", {}),
])
def test_admin_routes_reject_missing_and_user_tokens(client, path, method, kwargs):
    request = getattr(client, method)
    assert request(path, **kwargs).status_code == 401
    assert request(path, headers=bearer(USER_1_TOKEN), **kwargs).status_code == 403


def test_admin_token_passes_boundary(client):
    # Service globals are intentionally uninitialized; 503 means auth passed.
    assert client.post("/skills/reload", headers=bearer(ADMIN_TOKEN)).status_code == 503
    assert client.post("/eval/run", headers=bearer(ADMIN_TOKEN)).status_code == 503


def test_chat_identity_is_bound_to_token(client):
    payload = {"message": "VPN 不通", "user_id": "u2"}
    assert client.post("/chat", json=payload, headers=bearer(USER_1_TOKEN)).status_code == 403
    payload["user_id"] = "u1"
    assert client.post("/chat", json=payload, headers=bearer(USER_1_TOKEN)).status_code == 503
    assert client.post("/chat", json=payload).status_code == 401


@pytest.mark.parametrize("path,method", [
    ("/search?query=vpn", "post"),
    ("/knowledge/stats", "get"),
    ("/trace/tool/abc", "get"),
])
def test_user_routes_reject_missing_tokens(client, path, method):
    assert getattr(client, method)(path).status_code == 401


def test_cors_allows_local_frontend_but_not_other_origins(client):
    headers = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    allowed = client.request("OPTIONS", "/chat", headers={**headers, "Origin": "http://localhost:5174"})
    denied = client.request("OPTIONS", "/chat", headers={**headers, "Origin": "https://other.example"})
    assert allowed.status_code == 200
    assert denied.status_code == 400


def test_archive_identity_is_bound_to_token(client, monkeypatch):
    class Memory:
        async def archive_conversation(self, user_id, conv_id):
            return {"archived": True, "user_id": user_id, "conv_id": conv_id}

    monkeypatch.setattr(main, "_memory", Memory())
    payload = {"user_id": "u2", "conv_id": "c1"}
    assert client.post("/conversation/archive", json=payload, headers=bearer(USER_1_TOKEN)).status_code == 403
    payload.pop("user_id")
    response = client.post("/conversation/archive", json=payload, headers=bearer(USER_1_TOKEN))
    assert response.status_code == 200
    assert response.json()["user_id"] == "u1"


def test_tool_trace_is_visible_only_to_its_owner(client, monkeypatch):
    class Orchestrator:
        def get_tool_trace(self, request_id):
            return {"request_id": request_id, "user_id": "u1", "tool_calls": [{"input": "private"}]}

    monkeypatch.setattr(main, "_orchestrator", Orchestrator())
    own = client.get("/trace/tool/abc", headers=bearer(USER_1_TOKEN))
    other = client.get("/trace/tool/abc", headers=bearer(USER_2_TOKEN))
    assert own.status_code == 200 and own.json()["found"] is True
    assert other.status_code == 200 and other.json() == {"request_id": "abc", "found": False, "trace": {}}


def test_orchestrator_records_trace_owner():
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator._recent_tool_traces = deque(maxlen=2)
    result = OrchestratorResult(request_id="abc", response="ok", agent_type=AgentType.GENERAL, intent=None)
    orchestrator._record_tool_trace(result, "u1")
    assert orchestrator.get_tool_trace("abc")["user_id"] == "u1"
