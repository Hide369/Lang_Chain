"""実行前の git pull の対象を確かめる。

git は、渡したフォルダに .git がなければ親フォルダをたどって見つけたリポジトリを使う。
そのため、git 管理されていないフォルダを選ぶと、上位にある別のリポジトリを pull してしまうことがある。
これを防ぐため、対象フォルダがクローンしたリポジトリの最上位フォルダであることと、
pull 先（追跡ブランチ）が設定されていることを、pull の前に確かめる。
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 30
# URL に含まれる認証情報（https://user:token@host/...）
_USERINFO_RE = re.compile(r"(?<=://)[^/@]+@")


class GitRepoError(ValueError):
    """対象フォルダが pull できるリポジトリではない。"""


@dataclass(frozen=True)
class GitRepoInfo:
    toplevel: Path  # リポジトリの最上位フォルダ
    branch: str  # いまのブランチ（例: main）
    upstream: str  # pull 先の追跡ブランチ（例: origin/main）
    remote: str  # pull 先のリモート名（例: origin）
    url: str  # pull 先の URL（認証情報は伏せる）

    def describe(self) -> str:
        return f"pull 先: {self.url}（{self.upstream}）  ブランチ: {self.branch}  リポジトリ: {self.toplevel}"


def redact_url(url: str) -> str:
    """ログに出せるよう、URL の認証情報（ユーザー名・トークン）を伏せる。"""
    return _USERINFO_RE.sub("***@", url)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as e:
        raise GitRepoError("git コマンドが見つかりません") from e
    except subprocess.TimeoutExpired as e:
        raise GitRepoError(f"git の実行が {GIT_TIMEOUT_S} 秒で終わりませんでした") from e


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return a.resolve() == b.resolve()


def inspect_repo(repo: Path) -> GitRepoInfo:
    """対象フォルダが pull できるリポジトリかを確かめ、pull 先を返す。できなければ GitRepoError。"""
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise GitRepoError(f"対象フォルダは git リポジトリではありません: {repo}")
    toplevel = Path(top.stdout.strip())
    if not _same_dir(toplevel, repo):
        raise GitRepoError(
            f"対象フォルダが、クローンしたリポジトリの最上位フォルダではありません（対象フォルダ: {repo}、"
            f"git が使うリポジトリ: {toplevel}）。意図しないリポジトリを pull しないよう、"
            "クローンしたフォルダそのものを対象フォルダに選ぶか、「実行前に git pull」をオフにしてください"
        )

    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode != 0:
        raise GitRepoError(f"ブランチではなく特定のコミットを取り出しているため、pull できません（detached HEAD）: {repo}")
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        raise GitRepoError(
            f"ブランチ {branch.stdout.strip()} に pull 先（追跡ブランチ）が設定されていません: {repo}"
            "（git branch --set-upstream-to=origin/<ブランチ> で設定してください）"
        )
    remote = _git(repo, "config", "--get", f"branch.{branch.stdout.strip()}.remote").stdout.strip() or "origin"
    url = _git(repo, "remote", "get-url", remote)
    return GitRepoInfo(
        toplevel=toplevel,
        branch=branch.stdout.strip(),
        upstream=upstream.stdout.strip(),
        remote=remote,
        url=redact_url(url.stdout.strip()) if url.returncode == 0 else f"（リモート {remote} の URL を取得できません）",
    )
