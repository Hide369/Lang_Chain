"""Claude Code（ヘッドレスモード `claude -p`）で設計書を生成するアダプター。

モデルは Ollama の Anthropic 互換APIで動かすため、ソースコードはサーバーの外に出ない。
doc_graph からは `ainvoke(messages) -> AIMessage` を持つチャットモデルとして扱える。
再生成では直前の応答に残したセッションIDで `--resume` し、エージェントが読んだ内容を引き継ぐ。
"""

import asyncio
import json
import os
import shlex
import shutil
from pathlib import Path

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from .config import Settings

# 設計書の保存は runner が行うので、エージェントには読み取り系のツールだけを与える
_READ_ONLY_TOOLS = "Read,Glob,Grep"
_STDERR_TAIL_CHARS = 2000


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeCodeLLM:
    def __init__(
        self,
        command: list[str],
        *,
        model: str,
        ollama_base_url: str,
        cwd: Path,
        add_dirs: list[Path],
        max_turns: int,
        max_output_tokens: int,
        timeout_s: float,
    ):
        self.command = command
        self.model = model
        self.ollama_base_url = ollama_base_url
        self.cwd = cwd
        self.add_dirs = add_dirs
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens
        self.timeout_s = timeout_s

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "ANTHROPIC_BASE_URL": self.ollama_base_url,
            "ANTHROPIC_AUTH_TOKEN": "ollama",  # 必須だがOllamaは値を見ない
            "ANTHROPIC_API_KEY": "",
            # Claude Code は補助処理に別モデル(haiku等)を使うため、すべてローカルモデルに向ける
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.model,
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_output_tokens),
            # テレメトリ等、モデル以外への通信を止める
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        return env

    def _args(self, messages: list[AnyMessage]) -> list[str]:
        if not messages or not isinstance(messages[-1], HumanMessage):
            raise ValueError("最後のメッセージは HumanMessage である必要があります")
        prompt = messages[-1].content
        last_reply = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        session_id = last_reply.response_metadata.get("session_id") if last_reply else None

        # プロンプトは -p の直後に置く（--add-dir など複数値を取るオプションに吸い込まれないように）
        args = [
            *self.command, "-p", prompt,
            "--output-format", "json",
            "--model", self.model,
            "--tools", _READ_ONLY_TOOLS,
            "--strict-mcp-config",  # 利用者の設定にあるMCPサーバーを読み込まない
            "--max-turns", str(self.max_turns),
        ]
        for d in self.add_dirs:
            args += ["--add-dir", str(d)]
        if session_id:
            args += ["--resume", session_id]  # システムプロンプトはセッションに残っている
        else:
            system = "\n\n".join(m.content for m in messages if isinstance(m, SystemMessage))
            if system:
                args += ["--append-system-prompt", system]
        return args

    async def ainvoke(self, messages: list[AnyMessage], config=None, **kwargs) -> AIMessage:
        proc = await asyncio.create_subprocess_exec(
            *self._args(messages),
            cwd=self.cwd,
            env=self._env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ClaudeCodeError(f"Claude Code が {self.timeout_s} 秒以内に終了しませんでした") from None

        stderr_tail = stderr.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:]
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            raise ClaudeCodeError(
                f"Claude Code の出力をJSONとして読めません (exit={proc.returncode}): {stderr_tail}"
            ) from None
        if proc.returncode != 0 or data.get("is_error"):
            # 例: subtype=error_max_turns（CLAUDE_MAX_TURNS に達した）
            raise ClaudeCodeError(
                f"Claude Code がエラー終了しました (exit={proc.returncode}, subtype={data.get('subtype')}): "
                f"{data.get('result') or stderr_tail}"
            )

        usage = data.get("usage") or {}
        return AIMessage(
            content=data.get("result") or "",
            response_metadata={
                "session_id": data.get("session_id"),
                "num_turns": data.get("num_turns"),
                "output_tokens": usage.get("output_tokens"),
            },
        )


def build_claude_code(settings: Settings, repo: Path, skill_dir: Path) -> ClaudeCodeLLM:
    command = shlex.split(settings.claude_bin)
    if not command or shutil.which(command[0]) is None:
        raise RuntimeError(f"Claude Code が見つかりません: {settings.claude_bin!r}（CLAUDE_BIN を確認してください）")
    return ClaudeCodeLLM(
        command,
        model=settings.model,
        ollama_base_url=settings.ollama_base_url,
        cwd=repo,
        add_dirs=[skill_dir],
        max_turns=settings.claude_max_turns,
        max_output_tokens=settings.num_predict,
        timeout_s=settings.claude_timeout_s,
    )
