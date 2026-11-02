"""バッチドライバー: Goファイルごとに独立したthread_idでdoc_graphを実行する（方式A）。

使い方:
    python -m godoc_agent.runner --repo /path/to/go-repo --out ./out --limit 5
    python -m godoc_agent.runner --repo /path/to/go-repo --out ./out --run-id 20261102-2100  # 中断した実行を再開
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .claude_code import build_claude_code
from .config import Settings, load_settings
from .doc_graph import build_doc_graph
from .go_source import discover_go_files, sha256_text
from .preflight import preflight
from .prompts import build_agent_messages, load_skill

MANIFEST_NAME = ".manifest.json"


@dataclass
class FileResult:
    rel_path: str
    source_hash: str
    thread_id: str
    status: str
    duration_s: float
    attempts: int = 0
    issues: list[str] | None = None
    llm_calls: list[dict] | None = None
    error: str | None = None
    draft: str | None = field(default=None, repr=False)

    def to_log(self) -> dict:
        # 設計書本文は出力ファイルにあるので、計測ログには含めない
        return {k: v for k, v in asdict(self).items() if k != "draft"}


def thread_id_for(run_id: str, rel_path: str) -> str:
    # パスから決まる値にすることで、同じrun_idで再実行したとき同じセッションを再開できる
    return f"{run_id}:{hashlib.sha1(rel_path.encode('utf-8')).hexdigest()[:12]}"


def output_path_for(out_dir: Path, rel_path: str) -> Path:
    return out_dir / f"{rel_path}.md"


class Manifest:
    """生成済みファイルのソースハッシュ。変更のないファイルを次回以降スキップするために使う。"""

    def __init__(self, path: Path):
        self.path = path
        self.hashes: dict[str, str] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def is_up_to_date(self, rel_path: str, source_hash: str, out_dir: Path) -> bool:
        return self.hashes.get(rel_path) == source_hash and output_path_for(out_dir, rel_path).is_file()

    def record(self, rel_path: str, source_hash: str) -> None:
        self.hashes[rel_path] = source_hash
        # 途中で落ちても進捗が残るよう、1件ごとに書き出す（一時ファイル経由で破損を防ぐ）
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.hashes, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


async def process_file(
    graph, sem: asyncio.Semaphore, run_id: str, rel_path: str, source: str, source_hash: str
) -> FileResult:
    thread_id = thread_id_for(run_id, rel_path)
    config = {"configurable": {"thread_id": thread_id}, "run_name": f"design-doc:{rel_path}"}
    async with sem:
        started = time.perf_counter()  # 待ち時間を含めず、処理時間だけを計測する
        try:
            snapshot = await graph.aget_state(config)
            if snapshot.next:
                state = await graph.ainvoke(None, config)  # 中断したセッションを途中から再開
            elif snapshot.values.get("status") == "done":
                state = snapshot.values  # このrun_idで生成済み（出力の書き出しだけ行う）
            else:
                state = await graph.ainvoke({"rel_path": rel_path, "source": source}, config)
        except Exception as e:  # 1件の失敗でバッチ全体を止めず、記録して次へ進む
            return FileResult(
                rel_path, source_hash, thread_id, "error", time.perf_counter() - started,
                error=f"{type(e).__name__}: {e}",
            )

    return FileResult(
        rel_path=rel_path,
        source_hash=source_hash,
        thread_id=thread_id,
        status=state.get("status", "error"),
        duration_s=time.perf_counter() - started,
        attempts=state.get("attempts", 0),
        issues=state.get("issues"),
        llm_calls=state.get("llm_calls"),
        draft=state.get("draft"),
    )


def summarize(results: list[FileResult], wall_s: float) -> str:
    counts = Counter(r.status for r in results)
    calls = [c for r in results for c in (r.llm_calls or [])]
    turns = [c["num_turns"] for c in calls if c.get("num_turns")]
    done = [r for r in results if r.status == "done"]
    lines = [
        f"対象: {len(results)} 件 / 経過時間: {wall_s / 60:.1f} 分",
        "結果: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        f"Claude Code 実行: {len(calls)} 回 / 平均 {sum(turns) / len(turns):.1f} ターン" if turns else f"Claude Code 実行: {len(calls)} 回",
    ]
    if done:
        lines.append(f"成功1件あたり平均: {sum(r.duration_s for r in done) / len(done):.0f} 秒")
    if results:
        lines.append(f"失敗率: {(len(results) - len(done)) / len(results):.1%}")
    return "\n".join(lines)


async def run(args: argparse.Namespace, settings: Settings) -> int:
    repo: Path = args.repo.resolve()
    out_dir: Path = args.out.resolve()
    if not repo.is_dir():
        raise SystemExit(f"--repo がディレクトリではありません: {repo}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_runs").mkdir(exist_ok=True)
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)

    # 設定ミス（SKILL.md・claudeコマンドがない）は対象ファイルの走査より前に検出する
    skill = load_skill(settings.claude_skill_path)
    skill_dir = settings.claude_skill_path.parent
    llm = build_claude_code(settings, repo, skill_dir)
    manifest = Manifest(out_dir / MANIFEST_NAME)

    targets: list[tuple[str, str, str]] = []
    skipped = 0
    for path in discover_go_files(repo):
        rel_path = path.relative_to(repo).as_posix()
        source = path.read_text(encoding="utf-8")
        source_hash = sha256_text(source)
        if not args.force and manifest.is_up_to_date(rel_path, source_hash, out_dir):
            skipped += 1
            continue
        targets.append((rel_path, source, source_hash))
    if args.limit:
        targets = targets[: args.limit]

    print(f"run_id={args.run_id} 対象={len(targets)} 件（変更なしでスキップ={skipped} 件）", flush=True)
    if not targets:
        return 0

    await preflight(settings)
    metrics_path = out_dir / "_runs" / f"{args.run_id}.jsonl"
    sem = asyncio.Semaphore(settings.concurrency)
    results: list[FileResult] = []
    started = time.perf_counter()

    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as checkpointer:
        graph = build_doc_graph(
            llm,
            partial(build_agent_messages, skill, skill_dir),
            max_source_chars=settings.max_source_chars,
            max_attempts=settings.max_attempts,
        ).compile(checkpointer=checkpointer)

        tasks = [
            asyncio.create_task(process_file(graph, sem, args.run_id, rel_path, source, source_hash))
            for rel_path, source, source_hash in targets
        ]
        with metrics_path.open("a", encoding="utf-8") as metrics_file:
            for i, finished in enumerate(asyncio.as_completed(tasks), start=1):
                result = await finished
                if result.status == "done" and result.draft:
                    out_path = output_path_for(out_dir, result.rel_path)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(result.draft + "\n", encoding="utf-8")
                    manifest.record(result.rel_path, result.source_hash)
                results.append(result)
                metrics_file.write(json.dumps(result.to_log(), ensure_ascii=False) + "\n")
                metrics_file.flush()
                detail = f" ({result.error or '; '.join(result.issues or [])})" if result.status != "done" else ""
                print(f"[{i}/{len(targets)}] {result.status:<17} {result.duration_s:6.0f}s  {result.rel_path}{detail}", flush=True)

    print(summarize(results, time.perf_counter() - started), flush=True)
    print(f"計測ログ: {metrics_path}", flush=True)
    return 0 if all(r.status == "done" for r in results) else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Goソースから詳細設計書を一括生成する")
    p.add_argument("--repo", type=Path, required=True, help="Goリポジトリのルート")
    p.add_argument("--out", type=Path, required=True, help="設計書の出力先")
    p.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"), help="同じ値を指定すると中断した実行を再開")
    p.add_argument("--limit", type=int, default=0, help="処理件数の上限（PoC用）")
    p.add_argument("--force", action="store_true", help="変更のないファイルも再生成する")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    try:
        return asyncio.run(run(parse_args(argv), load_settings()))
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        # 設定ミス・接続不可など、利用者が対処すべきエラーはトレースバックなしで示す
        print(f"エラー: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
