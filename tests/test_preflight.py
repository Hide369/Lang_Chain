import dataclasses

import httpx
import pytest

from godoc_agent.config import load_settings
from godoc_agent.preflight import preflight

BASE_URL = "http://127.0.0.1:8000"


def settings_for(model: str):
    return dataclasses.replace(load_settings(), llm_base_url=BASE_URL, model=model)


def client_returning(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def models_response(*ids: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE_URL}/v1/models"
        return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})

    return handler


async def test_passes_when_vllm_serves_the_alias():
    # vLLM の --served-model-name で付けた別名がそのまま一覧に出る
    async with client_returning(models_response("claude-sonnet-4-5")) as client:
        await preflight(settings_for("claude-sonnet-4-5"), client=client)


async def test_passes_for_ollama_tag_without_latest_suffix():
    # Ollama に戻した場合、一覧には :latest 付きで出るが LLM_MODEL はタグなしで書いてある
    async with client_returning(models_response("qwen3-coder-next:latest")) as client:
        await preflight(settings_for("qwen3-coder-next"), client=client)


async def test_missing_model_lists_served_names():
    async with client_returning(models_response("qwen3-coder-next")) as client:
        with pytest.raises(RuntimeError, match="claude-sonnet-4-5.*qwen3-coder-next"):
            await preflight(settings_for("claude-sonnet-4-5"), client=client)


async def test_unreachable_server_is_reported_with_url():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with client_returning(handler) as client:
        with pytest.raises(RuntimeError, match="接続できません.*8000"):
            await preflight(settings_for("claude-sonnet-4-5"), client=client)


async def test_http_error_is_reported():
    async with client_returning(lambda r: httpx.Response(500, text="boom")) as client:
        with pytest.raises(RuntimeError, match="500"):
            await preflight(settings_for("claude-sonnet-4-5"), client=client)


async def test_unexpected_body_is_reported():
    async with client_returning(lambda r: httpx.Response(200, json={"models": []})) as client:
        with pytest.raises(RuntimeError, match="/v1/models"):
            await preflight(settings_for("claude-sonnet-4-5"), client=client)
