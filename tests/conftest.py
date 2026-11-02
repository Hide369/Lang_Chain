from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

TEMPLATE = """# 詳細設計書

<!-- ## これはコメント内なので必須セクションではない -->

## 1. 概要
## 2. 関数・メソッド仕様
"""

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


def reply(content: str, *, done_reason: str = "stop", prompt_tokens: int = 1000) -> AIMessage:
    return AIMessage(
        content=content,
        response_metadata={
            "done_reason": done_reason,
            "prompt_eval_count": prompt_tokens,
            "eval_count": 500,
            "eval_duration": 10 * 10**9,
        },
    )


class ScriptedLLM:
    """Ollamaの代わりに、用意した応答を順番に返す。受け取ったメッセージも記録する。"""

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


@pytest.fixture
def template_file(tmp_path: Path) -> Path:
    path = tmp_path / "template.md"
    path.write_text(TEMPLATE, encoding="utf-8")
    return path
