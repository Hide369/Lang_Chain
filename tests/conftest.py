import json
import shlex
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

SAMPLE_GO = """package user

import (
\t"context"
\t"errors"
)

// func Commented() はコメントなので抽出しない
type Repository interface {
\tFind(ctx context.Context, id string) (*User, error)
}

type User struct {
\tID   string
\tName string
}

func NewService(repo Repository) *Service { return &Service{repo: repo} }

func (s *Service) GetUser(ctx context.Context, id string) (*User, error) {
\tif id == "" {
\t\treturn nil, errors.New("empty id")
\t}
\treturn s.repo.Find(ctx, id)
}

func validate(id string) bool { return id != "" }
"""


def good_doc(*identifiers: str) -> str:
    body = " ".join(identifiers)
    return f"# 詳細設計書\n\n## 1. 概要\n説明 {body}\n\n## 2. 関数・メソッド仕様\n詳細\n"


def reply(content: str, *, input_tokens: int | None = None) -> AIMessage:
    """ClaudeCodeLLM が返す応答と同じ形。"""
    return AIMessage(
        content=content,
        response_metadata={
            "num_turns": 3, "output_tokens": 500,
            "input_tokens": input_tokens, "cache_read_input_tokens": None, "cache_creation_input_tokens": None,
        },
    )


class ScriptedLLM:
    """ClaudeCodeLLM の代わりに、用意した応答を順番に返す。受け取ったメッセージも記録する。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list] = []

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("想定より多くLLMが呼ばれました")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def claude_result(
    result: str, *, session_id: str = "s1", is_error: bool = False, subtype: str = "success", usage: dict | None = None
) -> dict:
    """`claude -p --output-format json` の出力を模した応答。"""
    return {"json": {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "session_id": session_id,
        "num_turns": 3,
        "usage": usage if usage is not None else {"input_tokens": 20000, "output_tokens": 500},
    }}


class FakeClaude:
    """tests/fake_claude.py を claude コマンドの代わりに使う。"""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.command = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]

    @property
    def bin(self) -> str:
        return shlex.join(self.command)  # 環境変数 CLAUDE_BIN に入れる形式

    def respond(self, *responses: dict) -> None:
        (self.state_dir / "responses.json").write_text(json.dumps(list(responses)), encoding="utf-8")

    def calls(self) -> list[dict]:
        path = self.state_dir / "calls.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch) -> FakeClaude:
    state_dir = tmp_path / "fake_claude"
    state_dir.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(state_dir))
    return FakeClaude(state_dir)
