"""画面で選んだ対象フォルダ（--repo）と出力先フォルダ（--out）の保存と、フォルダ選択ダイアログ用の一覧。

どちらも WEB_BROWSE_ROOTS の下からしか選べない（任意のパスを runner に渡させないため）。

複数人が同時に使えるよう、選んだフォルダはブラウザごと（Cookie の利用者ID ごと）に
`<state>/workspaces/<利用者ID>.json` へ保存する。まだ選んでいないブラウザには、共有の既定値
（`<state>/workspace.json`。最後にどこかのブラウザで選ばれたフォルダ）を表示する。
"""

import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

# ダイアログに一度に返すフォルダ数の上限（巨大なフォルダで応答が重くならないように）
MAX_ENTRIES = 1000
# 利用者ID（Cookie）の形式。ファイル名に使うため、これ以外は受け付けない
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@dataclass(frozen=True)
class Workspace:
    repo: str | None = None
    out: str | None = None


class WorkspaceStore:
    def __init__(self, state_dir: Path, browse_roots: tuple[Path, ...], extra_roots: tuple[Path, ...] = ()):
        self.path = state_dir / "workspace.json"  # 共有の既定値
        self.clients_dir = state_dir / "workspaces"
        self.roots = browse_roots
        # フォルダ選択ダイアログには出さないが、対象・出力先として認める範囲（クライアント PC からのアップロード先）
        self.extra_roots = extra_roots
        self._lock = threading.Lock()

    def _path_for(self, client_id: str | None) -> Path:
        if client_id is None:
            return self.path
        if not CLIENT_ID_RE.match(client_id):
            raise ValueError(f"利用者IDが正しくありません: {client_id!r}")
        return self.clients_dir / f"{client_id}.json"

    @staticmethod
    def _read(path: Path) -> Workspace | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Workspace(repo=data.get("repo"), out=data.get("out"))
        except (json.JSONDecodeError, AttributeError):
            return None

    def get(self, client_id: str | None = None) -> Workspace:
        """そのブラウザで選んだフォルダ。まだ選んでいなければ共有の既定値。"""
        if client_id is not None and (own := self._read(self._path_for(client_id))) is not None:
            return own
        return self._read(self.path) or Workspace()

    def set(self, repo: str, out: str, client_id: str | None = None, *, share: bool = True) -> Workspace:
        repo_path = self.check_repo(repo)
        out_path = self.check_out(out)
        if repo_path == out_path or out_path.is_relative_to(repo_path):
            raise ValueError("出力先は対象フォルダの外にしてください（生成物が次回の対象に混ざるのを防ぐため）")
        workspace = Workspace(repo=str(repo_path), out=str(out_path))
        # 最後に選ばれたフォルダを共有の既定値にもする（まだ選んでいないブラウザの初期値・
        # フォルダの記録がない以前の定期実行の設定で使う。以前の版と同じ動き）
        # （アップロードしたフォルダは、そのブラウザの人のものなので共有の既定値にしない: share=False）
        paths = dict.fromkeys([self._path_for(client_id), *([self.path] if share else [])])
        with self._lock:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(asdict(workspace), ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
        return workspace

    # --- 検証 ---------------------------------------------------------------------------

    def resolve_within_roots(self, raw: str) -> Path:
        if not raw or not Path(raw).is_absolute():
            raise ValueError(f"フォルダは絶対パスで指定してください: {raw!r}")
        path = Path(raw).resolve()
        if not any(path == root or path.is_relative_to(root) for root in (*self.roots, *self.extra_roots)):
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
