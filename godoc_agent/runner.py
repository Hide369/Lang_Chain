"""バッチドライバー: ソースファイル×タスクごとに独立したthread_idでdoc_graphを実行する（方式A）。

タスクは設計書の生成(doc)とコメント追記(comment)。既定では、スキルが設定されているタスクをすべて実行する。
対象の言語・フォルダ・ファイルは targets.toml（なければ targets.example.toml）で決まる。

使い方:
    python -m godoc_agent.runner --repo /path/to/repo --out ./out --limit 5
    python -m godoc_agent.runner --repo /path/to/repo --out ./out --run-id 20261102-2100  # 中断した実行を再開
    python -m godoc_agent.runner --repo /path/to/repo --out ./out --tasks comment         # コメント追記だけ
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .claude_code import build_claude_code
from .config import Settings, load_settings
from .doc_graph import build_doc_graph
from .source_common import sha256_text
from .preflight import preflight
from .prompts import load_skill
from .targets import discover_source_files
from .tasks import COMMENT, DOC, TASK_NAMES, Task, comment_task, design_doc_task


@dataclass
class FileResult:
    task: str
    rel_path: str
    source_hash: str
    thread_id: str
    status: str
    duration_s: float
    attempts: int = 0
    issues: list[str] | None = None
    # 合否には影響しない注意（コンテキスト上限への接近など）。issues と違い done でも残る
    warnings: list[str] | None = None
    llm_calls: list[dict] | None = None
    error: str | None = None
    draft: str | None = field(default=None, repr=False)

    def to_log(self) -> dict:
        # 生成物の本文は出力ファイルにあるので、計測ログには含めない
        return {k: v for k, v in asdict(self).items() if k != "draft"}


def thread_id_for(run_id: str, task: str, rel_path: str) -> str:
    # パスから決まる値にすることで、同じrun_idで再実行したとき同じセッションを再開できる
    return f"{run_id}:{task}:{hashlib.sha1(rel_path.encode('utf-8')).hexdigest()[:12]}"


class Manifest:
    """生成済みファイルのソースハッシュ。変更のないファイルを次回以降スキップするために使う。"""

    def __init__(self, path: Path):
        self.path = path
        self.hashes: dict[str, str] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def is_up_to_date(self, rel_path: str, source_hash: str, output_path: Path) -> bool:
        return self.hashes.get(rel_path) == source_hash and output_path.is_file()

    def record(self, rel_path: str, source_hash: str) -> None:
        self.hashes[rel_path] = source_hash
        # 途中で落ちても進捗が残るよう、1件ごとに書き出す（一時ファイル経由で破損を防ぐ）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.hashes, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


@dataclass
class TaskRun:
    """1つのタスクを実行するための部品一式（タスク定義・コンパイル済みグラフ・差分記録）。"""

    task: Task
    graph: object
    manifest: Manifest


async def process_file(
    run: TaskRun, sem: asyncio.Semaphore, run_id: str, rel_path: str, language: str, source: str, source_hash: str,
    out_dir: Path,
) -> FileResult:
    task = run.task
    thread_id = thread_id_for(run_id, task.name, rel_path)
    config = {"configurable": {"thread_id": thread_id}, "run_name": f"{task.run_name}:{rel_path}"}
    async with sem:
        started = time.perf_counter()  # 待ち時間を含めず、処理時間だけを計測する
        try:
            snapshot = await run.graph.aget_state(config)
            if snapshot.next:
                state = await run.graph.ainvoke(None, config)  # 中断したセッションを途中から再開
            elif snapshot.values.get("status") == "done":
                state = snapshot.values  # このrun_idで生成済み（出力の書き出しだけ行う）
            else:
                # 既存の設計書があれば渡し、履歴（バージョン）を引き継いで改訂させる
                previous_path = task.output_path(out_dir, rel_path)
                previous_output = (
                    previous_path.read_text(encoding="utf-8")
                    if task.uses_previous_output and previous_path.is_file()
                    else None
                )
                state = await run.graph.ainvoke(
                    {"rel_path": rel_path, "language": language, "source": source, "previous_output": previous_output}, config
                )
        except Exception as e:  # 1件の失敗でバッチ全体を止めず、記録して次へ進む
            return FileResult(
                task=task.name, rel_path=rel_path, source_hash=source_hash, thread_id=thread_id, status="error",
                duration_s=time.perf_counter() - started, error=f"{type(e).__name__}: {e}",
            )

    return FileResult(
        task=task.name,
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


def context_warnings(llm_calls: list[dict] | None, settings: Settings) -> list[str]:
    """入力トークン数が LLM_NUM_CTX の LLM_CTX_WARN_RATIO を超えた実行を警告にする。

    Ollama はコンテキスト超過分を黙って切り捨てるため、検証に合格していても入力の一部が
    読まれていない可能性がある。合否の判定はグラフに任せ、ここでは注意として残すだけにする。
    """
    warnings = []
    for i, call in enumerate(llm_calls or [], start=1):
        total = call.get("total_input_tokens")
        if total is not None and total > settings.ctx_warn_tokens:
            warnings.append(
                f"{i} 回目の実行の入力が {total} トークンで、LLM_NUM_CTX={settings.num_ctx} の "
                f"{settings.ctx_warn_ratio:.0%} を超えています（入力が切り捨てられた可能性があります）"
            )
    return warnings


def summarize(results: list[FileResult], wall_s: float) -> str:
    calls = [c for r in results for c in (r.llm_calls or [])]
    turns = [c["num_turns"] for c in calls if c.get("num_turns")]
    # usage を返さない実行（不明）は除いて集計する
    input_tokens = [c["total_input_tokens"] for c in calls if c.get("total_input_tokens") is not None]
    warned = [r for r in results if r.warnings]
    done = [r for r in results if r.status == "done"]
    lines = [f"対象: {len(results)} 件 / 経過時間: {wall_s / 60:.1f} 分"]
    for task in dict.fromkeys(r.task for r in results):
        counts = Counter(r.status for r in results if r.task == task)
        lines.append(f"結果({task}): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append(
        f"Claude Code 実行: {len(calls)} 回 / 平均 {sum(turns) / len(turns):.1f} ターン" if turns else f"Claude Code 実行: {len(calls)} 回"
    )
    if input_tokens:
        lines.append(
            f"入力トークン(1回あたり): 最大 {max(input_tokens)} / 平均 {sum(input_tokens) / len(input_tokens):.0f}"
            f"（{len(calls)} 回中 {len(input_tokens)} 回で取得）"
        )
    if warned:
        lines.append(f"コンテキスト上限に近い実行があった件数: {len(warned)} 件（計測ログの warnings を参照）")
    if done:
        lines.append(f"成功1件あたり平均: {sum(r.duration_s for r in done) / len(done):.0f} 秒")
    if results:
        lines.append(f"失敗率: {(len(results) - len(done)) / len(results):.1%}")
    return "\n".join(lines)


def selected_tasks(requested: list[str] | None, settings: Settings) -> list[str]:
    if requested:
        return requested
    # 指定がなければ、設計書に加えて、コメント追記スキルが設定されていればそれも実行する
    return [DOC] + ([COMMENT] if settings.claude_comment_skill_path else [])


def build_task(name: str, settings: Settings, today: date) -> tuple[Task, Path]:
    if name == DOC:
        skill_path = settings.claude_skill_path
        skill = load_skill(skill_path, "CLAUDE_SKILL_PATH")
        return design_doc_task(skill, skill_path.parent, today, max_input_chars=settings.max_source_chars), skill_path.parent
    skill_path = settings.claude_comment_skill_path
    skill = load_skill(skill_path, "CLAUDE_COMMENT_SKILL_PATH")
    return comment_task(skill, skill_path.parent, today, max_input_chars=settings.max_comment_source_chars), skill_path.parent


async def run(args: argparse.Namespace, settings: Settings) -> int:
    repo: Path = args.repo.resolve()
    out_dir: Path = args.out.resolve()
    if not repo.is_dir():
        raise SystemExit(f"--repo がディレクトリではありません: {repo}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_runs").mkdir(exist_ok=True)
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)

    # エージェントは読み取り系ツールしか使えず日付を調べられないため、ここで渡す。
    # 実行中に日付をまたいでも、同じ実行の出力はすべて同じ日付になるよう一度だけ取得する
    today = date.today()
    # 設定ミス（SKILL.md・claudeコマンドがない）は対象ファイルの走査より前に検出する
    tasks = [build_task(name, settings, today) for name in selected_tasks(args.tasks, settings)]
    llms = {task.name: build_claude_code(settings, repo, skill_dir) for task, skill_dir in tasks}
    manifests = {task.name: Manifest(task.manifest_path(out_dir)) for task, _ in tasks}

    targets: list[tuple[Task, str, str, str, str]] = []
    skipped = 0
    for file in discover_source_files(repo, settings.targets):
        rel_path = file.path.relative_to(repo).as_posix()
        source = file.path.read_text(encoding="utf-8")
        source_hash = sha256_text(source)
        for task, _ in tasks:
            if not args.force and manifests[task.name].is_up_to_date(rel_path, source_hash, task.output_path(out_dir, rel_path)):
                skipped += 1
                continue
            targets.append((task, rel_path, file.language, source, source_hash))
    if args.limit:
        targets = targets[: args.limit]

    task_names = ",".join(task.name for task, _ in tasks)
    languages = ",".join(lang.name for lang in settings.targets.languages)
    print(f"対象の設定={settings.targets.source} 言語={languages}", flush=True)
    print(f"run_id={args.run_id} タスク={task_names} 対象={len(targets)} 件（変更なしでスキップ={skipped} 件）", flush=True)
    if not targets:
        return 0

    await preflight(settings)
    metrics_path = out_dir / "_runs" / f"{args.run_id}.jsonl"
    # 同時に動く claude の数はタスクをまたいで LLM_CONCURRENCY に抑える（Ollama の並列数に合わせるため）
    sem = asyncio.Semaphore(settings.concurrency)
    results: list[FileResult] = []
    started = time.perf_counter()

    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as checkpointer:
        runs = {
            task.name: TaskRun(
                task,
                build_doc_graph(llms[task.name], task, max_attempts=settings.max_attempts).compile(checkpointer=checkpointer),
                manifests[task.name],
            )
            for task, _ in tasks
        }
        jobs = [
            asyncio.create_task(process_file(runs[task.name], sem, args.run_id, rel_path, language, source, source_hash, out_dir))
            for task, rel_path, language, source, source_hash in targets
        ]
        with metrics_path.open("a", encoding="utf-8") as metrics_file:
            for i, finished in enumerate(asyncio.as_completed(jobs), start=1):
                result = await finished
                result.warnings = context_warnings(result.llm_calls, settings)
                if result.status == "done" and result.draft:
                    task_run = runs[result.task]
                    out_path = task_run.task.output_path(out_dir, result.rel_path)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(result.draft + "\n", encoding="utf-8")
                    task_run.manifest.record(result.rel_path, result.source_hash)
                results.append(result)
                metrics_file.write(json.dumps(result.to_log(), ensure_ascii=False) + "\n")
                metrics_file.flush()
                detail = f" ({result.error or '; '.join(result.issues or [])})" if result.status != "done" else ""
                if result.warnings:
                    detail += f" [警告: {'; '.join(result.warnings)}]"
                print(
                    f"[{i}/{len(targets)}] {result.status:<17} {result.duration_s:6.0f}s  {result.task:<7} {result.rel_path}{detail}",
                    flush=True,
                )

    print(summarize(results, time.perf_counter() - started), flush=True)
    print(f"計測ログ: {metrics_path}", flush=True)
    return 0 if all(r.status == "done" for r in results) else 1


def _task_list(value: str) -> list[str]:
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in TASK_NAMES]
    if not names or unknown:
        raise argparse.ArgumentTypeError(f"{', '.join(TASK_NAMES)} をカンマ区切りで指定してください: {value!r}")
    return list(dict.fromkeys(names))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ソースファイルから詳細設計書の生成・コメント追記を一括で行う")
    p.add_argument("--repo", type=Path, required=True, help="対象リポジトリのルート")
    p.add_argument("--out", type=Path, required=True, help="設計書の出力先（コメント追記済みのファイルは <out>/_commented/ 配下）")
    p.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"), help="同じ値を指定すると中断した実行を再開")
    p.add_argument(
        "--tasks", type=_task_list, default=None,
        help="実行するタスク（doc,comment）。省略時は doc と、CLAUDE_COMMENT_SKILL_PATH が設定されていれば comment",
    )
    p.add_argument("--limit", type=int, default=0, help="処理件数の上限（PoC用。ファイル×タスクの件数）")
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
