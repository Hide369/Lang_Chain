"""推論サーバー（vLLM / Ollama）の事前チェック。モデルの呼び出し自体は Claude Code（claude_code.py）が行う。

vLLM と Ollama のどちらも持つ OpenAI 互換の `/v1/models` でモデル名を確認する
（Ollama 固有の API を使うと vLLM で必ず失敗するため）。
"""

import httpx

from .config import Settings

_TIMEOUT_S = 30


def _served_names(body: object) -> list[str]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise ValueError("data がありません")
    return [str(item["id"]) for item in data if isinstance(item, dict) and "id" in item]


def _is_served(model: str, served: list[str]) -> bool:
    # Ollama は既定のタグを `:latest` 付きで返すが、LLM_MODEL はタグなしで書くことが多い
    return model in served or f"{model}:latest" in served


async def preflight(settings: Settings, client: httpx.AsyncClient | None = None) -> None:
    """バッチ開始前にサーバー到達性とモデルの存在を確認し、全件失敗を防ぐ。"""
    url = f"{settings.llm_base_url.rstrip('/')}/v1/models"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT_S)
    try:
        response = await client.get(url)
        response.raise_for_status()
        served = _served_names(response.json())
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"推論サーバーがエラーを返しました: {url} (HTTP {e.response.status_code})") from e
    except httpx.TransportError as e:
        raise RuntimeError(f"推論サーバーに接続できません: {settings.llm_base_url} ({e})") from e
    except ValueError as e:  # JSON でない・形式が違う
        raise RuntimeError(f"推論サーバーの {url} の応答を読めません ({e})") from e
    finally:
        if owns_client:
            await client.aclose()

    if not _is_served(settings.model, served):
        raise RuntimeError(
            f"モデル {settings.model!r} が推論サーバーにありません（提供中: {', '.join(served) or 'なし'}）。"
            ".env の LLM_MODEL を vLLM の --served-model-name（Ollama なら `ollama list` のタグ名）と揃えてください"
        )
