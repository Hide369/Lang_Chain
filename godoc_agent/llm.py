"""Ollama接続。推論エンジンを変える場合（vLLMなど）はこのファイルだけを差し替える。"""

import httpx
import ollama
from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama

from .config import Settings


def build_llm(settings: Settings) -> Runnable:
    llm = ChatOllama(
        model=settings.model,
        base_url=settings.ollama_base_url,
        # サーバー側の既定値に頼らず、リクエストごとに明示する（黙った切り捨て対策）
        num_ctx=settings.num_ctx,
        num_predict=settings.num_predict,
        temperature=0.2,
        keep_alive=-1,  # バッチの途中でモデルがアンロードされるのを防ぐ
        client_kwargs={"timeout": settings.timeout_s},
    )
    # 通信断・タイムアウト・サーバーエラーのみ指数バックオフで再試行する
    # （ollamaクライアントは接続失敗を httpx.ConnectError ではなく ConnectionError で送出する）
    return llm.with_retry(
        retry_if_exception_type=(ConnectionError, httpx.TransportError, ollama.ResponseError),
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )


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
