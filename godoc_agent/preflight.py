"""Ollamaの事前チェック。モデルの呼び出し自体は Claude Code（claude_code.py）が行う。"""

import httpx
import ollama

from .config import Settings


async def preflight(settings: Settings) -> None:
    """バッチ開始前にサーバー到達性とモデルの存在を確認し、全件失敗を防ぐ。"""
    client = ollama.AsyncClient(host=settings.ollama_base_url, timeout=30)
    try:
        await client.show(settings.model)
    except (ConnectionError, httpx.TransportError) as e:
        raise RuntimeError(f"Ollamaサーバーに接続できません: {settings.ollama_base_url} ({e})") from e
    except ollama.ResponseError as e:
        raise RuntimeError(
            f"モデル {settings.model!r} が見つかりません。`ollama list` でタグ名を確認してください ({e})"
        ) from e
