"""画面で選んだ対象フォルダ（--repo）と出力先フォルダ（--out）の保存と、フォルダ選択ダイアログ用の一覧。

どちらも WEB_BROWSE_ROOTS の下からしか選べない（任意のパスを runner に渡させないため）。
"""

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

# ダイアログに一度に返すフォルダ数の上限（巨大なフォルダで応答が重くならないように）
MAX_ENTRIES = 1000


@dataclass(frozen=True)
class Workspace:
    repo: str | None = None
    out: str | None = None


class WorkspaceStore:
    def __init__(self, state_dir: Path, browse_roots: tuple[Path, ...]):
        self.path = state_dir / "workspace.json"
        self.roots = browse_roots
        self._lock = threading.Lock()

    def get(self) -> Workspace:
        if not self.path.is_file():
            return Workspace()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Workspace(repo=data.get("repo"), out=data.get("out"))
        except (json.JSONDecodeError, AttributeError):
            return Workspace()

    def set(self, repo: str, out: str) -> Workspace:
        repo_path = self.check_repo(repo)
        out_path = self.check_out(out)
        if repo_path == out_path or out_path.is_relative_to(repo_path):
            raise ValueError("出力先は対象フォルダの外にしてください（生成物が次回の対象に混ざるのを防ぐため）")
        workspace = Workspace(repo=str(repo_path), out=str(out_path))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(workspace), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        return workspace

    # --- 検証 ---------------------------------------------------------------------------

    def resolve_within_roots(self, raw: str) -> Path:
        if not raw or not Path(raw).is_absolute():
            raise ValueError(f"フォルダは絶対パスで指定してください: {raw!r}")
        path = Path(raw).resolve()
        if not any(path == root or path.is_relative_to(root) for root in self.roots):
            raise PermissionError(f"選べる範囲（{', '.join(map(str, self.roots))}）の外です: {path}")
        return path

    def check_repo(self, raw: str) -> Path:
        path = self.resolve_within_roots(raw)
        if not path.is_dir():
            raise ValueError(f"対象フォルダがありません: {path}")
        return path

    def check_out(self, raw: str) -> Path:
        path = self.resolve_within_roots(raw)
        if path.exists() and not path.is_dir():
            raise ValueError(f"出力先がフォルダではありません: {path}")
        # 出力先は runner が作るので、まだなくてもよい。ただし親フォルダは必要
        if not path.exists() and not path.parent.is_dir():
            raise ValueError(f"出力先の親フォルダがありません: {path.parent}")
        return path

    # --- フォルダ選択ダイアログ ------------------------------------------------------------

    def browse(self, raw: str | None) -> dict:
        """フォルダの中のサブフォルダを返す。raw がなければ起点の一覧を返す。"""
        if not raw:
            return {"path": None, "parent": None, "dirs": [{"name": str(r), "path": str(r)} for r in self.roots], "truncated": False}
        path = self.resolve_within_roots(raw)
        if not path.is_dir():
            raise ValueError(f"フォルダがありません: {path}")
        dirs = []
        try:
            children = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except PermissionError as e:
            raise PermissionError(f"フォルダを開く権限がありません: {path}") from e
        for child in children:
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # 壊れたリンク・権限のないフォルダは表示しない
            if len(dirs) >= MAX_ENTRIES:
                break
        in_root = path in self.roots
        return {
            "path": str(path),
            # 起点より上には戻らない（起点の一覧に戻る）
            "parent": None if in_root else str(path.parent),
            "dirs": dirs,
            "truncated": len(dirs) >= MAX_ENTRIES,
        }
