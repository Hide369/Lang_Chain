"""画面からの対象設定（targets.toml）の読み書きと、対象ファイルのプレビュー。

保存先は runner と同じ（TARGETS_CONFIG、未設定なら <アプリ>/targets.toml）なので、
夜間バッチ・コマンドからの実行にも反映される。検証は runner と同じ処理（targets._parse_targets）で行う。
画面から保存すると、ファイル内のコメントは失われる（項目の値だけを書き出す）。
"""

import json
import os
import threading
import tomllib
from pathlib import Path

from ..analyzers import ANALYZERS
from ..config import targets_config_path
from ..targets import _parse_targets, discover_source_files

# プレビューで返す対象ファイルの件数の上限
PREVIEW_LIMIT = 300


def _toml_str(value: str) -> str:
    # JSON の文字列のエスケープは TOML の basic string としてもそのまま読める
    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def to_toml(data: dict) -> str:
    lines = [
        "# 処理対象の言語・フォルダ・ファイル・除外の設定（Web 画面から保存）。書き方は docs/other/target_files.md を参照。",
        "# 手で編集してもよいが、画面から保存し直すとコメントは消える。",
        "",
        "# すべての言語に共通の除外",
        f"exclude = {_toml_list(data.get('exclude', []))}",
    ]
    for lang in data["languages"]:
        lines += [
            "",
            "[[languages]]",
            f"name = {_toml_str(lang['name'])}",
            f"enabled = {'true' if lang.get('enabled', True) else 'false'}",
            f"extensions = {_toml_list(lang.get('extensions', []))}",
            f"include = {_toml_list(lang.get('include', []))}",
            f"exclude = {_toml_list(lang.get('exclude', []))}",
        ]
    return "\n".join(lines) + "\n"


def _clean_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where} は文字列の配列で指定してください")
    # 画面では1行に1つ書くので、空行と前後の空白は取り除く
    return [v.strip() for v in value if v.strip()]


def normalize(data: object) -> dict:
    """画面から受け取った設定を、targets.toml と同じ形に整える（未知の項目は検証で弾く）。"""
    if not isinstance(data, dict):
        raise ValueError("設定の形式が正しくありません")
    languages = data.get("languages")
    if not isinstance(languages, list):
        raise ValueError("languages は配列で指定してください")
    result = {"exclude": _clean_list(data.get("exclude", []), "exclude"), "languages": []}
    for i, lang in enumerate(languages):
        if not isinstance(lang, dict):
            raise ValueError(f"languages[{i}] の形式が正しくありません")
        item = dict(lang)
        for key in ("extensions", "include", "exclude"):
            if key in item:
                item[key] = _clean_list(item[key], f"languages[{i}].{key}")
        result["languages"].append(item)
    return result


class TargetsEditor:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self._lock = threading.Lock()

    @property
    def read_path(self) -> Path:
        """画面に表示する設定ファイル。runner がいま使うもの（targets.toml がなければ targets.example.toml）。

        TARGETS_CONFIG のファイルがまだない場合も、例の設定をひな形として表示する（保存するとそのパスに作られる）。
        """
        path = targets_config_path(self.app_dir)
        return path if path.is_file() else self.app_dir / "targets.example.toml"

    @property
    def write_path(self) -> Path:
        raw = os.getenv("TARGETS_CONFIG")
        return Path(raw) if raw else self.app_dir / "targets.toml"

    def get(self) -> dict:
        path = self.read_path
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        languages = [dict(lang) for lang in data.get("languages", []) if isinstance(lang, dict)]
        # 設定ファイルに書かれていない言語も、無効の状態で画面に出す（有効にするだけで使えるように）
        present = {lang.get("name") for lang in languages}
        languages += [
            {"name": name, "enabled": False, "extensions": [], "include": [], "exclude": []}
            for name in ANALYZERS if name not in present
        ]
        for lang in languages:
            lang.setdefault("enabled", True)
            lang.setdefault("exclude", [])
        return {
            "path": str(path),
            "write_path": str(self.write_path),
            "is_example": path.name == "targets.example.toml",
            "languages_available": list(ANALYZERS),
            "config": {"exclude": data.get("exclude", []), "languages": languages},
        }

    def validate(self, data: object) -> dict:
        config = normalize(data)
        _parse_targets(config, self.write_path)  # 誤りは ValueError（runner と同じメッセージ）
        return config

    def save(self, data: object) -> dict:
        config = self.validate(data)
        text = to_toml(config)
        # 書き出した内容が読み戻せて、同じ検証を通ることを確かめてから置き換える
        _parse_targets(tomllib.loads(text), self.write_path)
        with self._lock:
            path = self.write_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        return self.get()

    def preview(self, data: object, repo: Path) -> dict:
        """保存前の設定で、対象フォルダのどのファイルが処理されるかを返す。"""
        config = normalize(data)
        targets = _parse_targets(config, self.write_path)
        files = discover_source_files(repo, targets)
        return {
            "total": len(files),
            "files": [{"path": f.path.relative_to(repo).as_posix(), "language": f.language} for f in files[:PREVIEW_LIMIT]],
            "truncated": len(files) > PREVIEW_LIMIT,
        }
