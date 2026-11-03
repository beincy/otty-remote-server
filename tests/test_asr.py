import asyncio
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.asr import AsrRequest, AsrSettings, extract_text, transcribe
from otty_server.app import Settings, create_app
from otty_server.gateway import GatewaySettings, create_gateway_app
from tests.test_api import AUTH, TOKEN, FakeCLI
from tests.test_gateway import REGISTER_KEY, connected_tunnel

AUDIO = base64.b64encode(b"RIFF" + b"\x00" * 64).decode()
ASR_ON = AsrSettings(api_key="sk-test-key", endpoint="https://asr.example.com/gen")
ASR_OFF = AsrSettings(api_key="")


def local_app(asr=ASR_OFF):
    return create_app(Settings(token=TOKEN, host_pane_id="p_host"), FakeCLI(), asr)


def test_extract_text_shapes():
    assert extract_text({"output": {"text": "你好"}}) == "你好"
    assert extract_text({"output": {"sentence": {"text": "hi"}}}) == "hi"
    choices = {"output": {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}}
    assert extract_text(choices) == "ab"
    assert extract_text(None) == ""


def test_asr_settings_default_model(monkeypatch):
    monkeypatch.delenv("OTTY_ASR_MODEL", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env")
    settings = AsrSettings.from_env()
    assert settings.model == "qwen-audio-3.1-asr-flash"
    assert settings.enabled


def test_asr_request_rejects_bad_base64():
    with pytest.raises(ValueError):
        AsrRequest(audio="not base64 at all !!!!", format="wav")


def test_transcribe_sends_dashscope_payload():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"output": {"text": " 测试文本 "}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await transcribe(ASR_ON, AsrRequest(audio=AUDIO), client)

    result = asyncio.run(run())
    assert result.text == "测试文本"
    assert seen["auth"] == "Bearer sk-test-key"
    assert "qwen-audio-3.1-asr-flash" in seen["body"]
    assert "data:audio/wav;base64," in seen["body"]


def test_local_asr_requires_token():
    with TestClient(local_app(ASR_ON)) as client:
        assert client.post("/api/v1/asr", json={"audio": AUDIO}).status_code == 401


def test_local_asr_not_configured():
    with TestClient(local_app(ASR_OFF)) as client:
        res = client.post("/api/v1/asr", json={"audio": AUDIO}, headers=AUTH)
    assert res.status_code == 503


def test_local_asr_success(monkeypatch):
    async def fake_transcribe(settings, body, client=None):
        from gateway.asr import AsrResult

        return AsrResult(text="hello", model=settings.model)

    monkeypatch.setattr("otty_server.app.transcribe", fake_transcribe)
    with TestClient(local_app(ASR_ON)) as client:
        res = client.post("/api/v1/asr", json={"audio": AUDIO}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["text"] == "hello"


def test_gateway_asr_requires_online_instance_token():
    gw = create_gateway_app(GatewaySettings(register_key=REGISTER_KEY), ASR_ON)
    with TestClient(gw) as client:
        assert client.post("/api/v1/asr", json={"audio": AUDIO}).status_code == 401
        res = client.post("/api/v1/asr", json={"audio": AUDIO}, headers=AUTH)
        assert res.status_code == 401  # no instance online for this token


def test_gateway_asr_uses_gateway_key(monkeypatch):
    async def fake_transcribe(settings, body, client=None):
        from gateway.asr import AsrResult

        return AsrResult(text="from-gateway", model=settings.model)

    monkeypatch.setattr("gateway.app.transcribe", fake_transcribe)
    gw = create_gateway_app(GatewaySettings(register_key=REGISTER_KEY), ASR_ON)
    with TestClient(gw) as client, connected_tunnel(client, local_app(ASR_OFF)):
        res = client.post("/api/v1/asr", json={"audio": AUDIO}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["text"] == "from-gateway"


def test_gateway_asr_falls_back_to_local_server(monkeypatch):
    async def fake_transcribe(settings, body, client=None):
        from gateway.asr import AsrResult

        return AsrResult(text="from-local", model=settings.model)

    monkeypatch.setattr("otty_server.app.transcribe", fake_transcribe)
    gw = create_gateway_app(GatewaySettings(register_key=REGISTER_KEY), ASR_OFF)
    with TestClient(gw) as client, connected_tunnel(client, local_app(ASR_ON)):
        res = client.post("/api/v1/asr", json={"audio": AUDIO}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["text"] == "from-local"
