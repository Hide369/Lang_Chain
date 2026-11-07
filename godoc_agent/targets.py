"""処理対象の言語・フォルダ・ファイル・除外の設定（targets.toml）の読み込みと、対象ファイルの列挙。

パターンは glob 形式で、--repo からの相対パス（区切りは /）と照合する。
  *  … / を含まない任意の文字列     ?  … / 以外の1文字     [abc] … いずれか1文字
  ** … 0個以上のフォルダ（"**/" は先頭や途中、"/**" は末尾に書く。末尾の "/**" はその下のすべて）
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .analyzers import ANALYZERS

_TOP_KEYS = {"exclude", "languages"}
_LANGUAGE_KEYS = {"name", "enabled", "extensions", "include", "exclude"}


@dataclass(frozen=True)
class GlobPattern:
    pattern: str
    regex: re.Pattern[str]

    def matches(self, rel_path: str) -> bool:
        return self.regex.fullmatch(rel_path) is not None


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == n:
            out.append("/.+")  # フォルダの下のすべて（フォルダと同名のファイル自体は含めない）
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and (end := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1 : end]
            negate = body.startswith("!")
            body = re.escape(body[1:] if negate else body).replace(r"\-", "-")
            out.append(f"[^/{body}]" if negate else f"[{body}]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def compile_pattern(pattern: str, where: str) -> GlobPattern:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(f"{where} のパターンが空です")
    if "\\" in pattern or pattern.startswith("/") or ".." in pattern.split("/"):
        raise ValueError(f"{where} のパターンは / 区切りの相対パスで書いてください（\\・先頭の /・.. は不可）: {pattern!r}")
    return GlobPattern(pattern, _glob_to_regex(pattern))


@dataclass(frozen=True)
class LanguageTarget:
    name: str  # 解析に使う言語（analyzers.ANALYZERS のキー）
    extensions: tuple[str, ...]
    include: tuple[GlobPattern, ...]
    exclude: tuple[GlobPattern, ...]


@dataclass(frozen=True)
class TargetConfig:
    source: Path  # 読み込んだ設定ファイル（実行時の表示用）
    languages: tuple[LanguageTarget, ...]
    exclude: tuple[GlobPattern, ...] = ()  # すべての言語に共通の除外


@dataclass(frozen=True)
class SourceFile:
    path: Path
    language: str


def _string_list(value: object, where: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where} は文字列の配列で書いてください")
    if not value and not allow_empty:
        raise ValueError(f"{where} を1つ以上書いてください")
    return value


def _unknown_keys(table: dict, allowed: set[str], where: str) -> None:
    # 綴りの間違い（include を includes と書く等）で設定が黙って無視されるのを防ぐ
    if unknown := sorted(set(table) - allowed):
        raise ValueError(f"{where} に不明な項目があります: {', '.join(unknown)}（使える項目: {', '.join(sorted(allowed))}）")


def _parse_language(table: object, index: int) -> LanguageTarget | None:
    where = f"languages[{index}]"
    if not isinstance(table, dict):
        raise ValueError(f"{where} は [[languages]] の表で書いてください")
    _unknown_keys(table, _LANGUAGE_KEYS, where)
    name = table.get("name")
    if name not in ANALYZERS:
        raise ValueError(f"{where}.name には {', '.join(ANALYZERS)} のいずれかを書いてください: {name!r}")
    where = f"languages[{index}]（{name}）"
    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled は true / false で書いてください")
    if not enabled:
        return None

    extensions = _string_list(table.get("extensions"), f"{where}.extensions", allow_empty=False)
    for ext in extensions:
        if not re.fullmatch(r"\.[\w.]+", ext):
            raise ValueError(f"{where}.extensions は . で始まる拡張子で書いてください: {ext!r}")
    include = _string_list(table.get("include"), f"{where}.include", allow_empty=False)
    exclude = _string_list(table.get("exclude", []), f"{where}.exclude", allow_empty=True)
    return LanguageTarget(
        name=name,
        extensions=tuple(extensions),
        include=tuple(compile_pattern(p, f"{where}.include") for p in include),
        exclude=tuple(compile_pattern(p, f"{where}.exclude") for p in exclude),
    )


def load_targets(path: Path) -> TargetConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"対象の設定ファイルが見つかりません: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"対象の設定ファイルの書式が正しくありません: {path}: {e}") from e
    try:
        return _parse_targets(data, path)
    except ValueError as e:
        raise ValueError(f"対象の設定ファイル {path}: {e}") from e


def _parse_targets(data: dict, path: Path) -> TargetConfig:
    _unknown_keys(data, _TOP_KEYS, "設定ファイルの先頭")
    exclude = _string_list(data.get("exclude", []), "exclude", allow_empty=True)
    tables = data.get("languages")
    if not isinstance(tables, list) or not tables:
        raise ValueError("[[languages]] を1つ以上書いてください")

    names = [t.get("name") for t in tables if isinstance(t, dict)]
    if duplicated := sorted({n for n in names if isinstance(n, str) and names.count(n) > 1}):
        raise ValueError(f"同じ言語が複数あります: {', '.join(duplicated)}")
    languages = [lang for i, t in enumerate(tables) if (lang := _parse_language(t, i)) is not None]
    if not languages:
        raise ValueError("有効（enabled = true）な言語がありません")

    # 拡張子から言語を一意に決めるため、有効な言語どうしで拡張子が重ならないようにする
    seen: dict[str, str] = {}
    for lang in languages:
        for ext in lang.extensions:
            if ext in seen:
                raise ValueError(f"拡張子 {ext} が {seen[ext]} と {lang.name} の両方に書かれています")
            seen[ext] = lang.name
    return TargetConfig(
        source=path,
        languages=tuple(languages),
        exclude=tuple(compile_pattern(p, "exclude") for p in exclude),
    )


def discover_source_files(repo: Path, config: TargetConfig) -> list[SourceFile]:
    files: dict[Path, SourceFile] = {}  # 拡張子の書き方（.ts と .d.ts など）で同じファイルが2回見つかっても1件にする
    for lang in config.languages:
        excludes = config.exclude + lang.exclude
        for ext in lang.extensions:
            for path in repo.rglob(f"*{ext}"):
                if not path.is_file():
                    continue
                rel = path.relative_to(repo).as_posix()
                # --repo に xx_api 自体を指定した場合も対象を判定できるよう、--repo のフォルダ名を付けたパスとも照合する
                if not any(p.matches(rel) or p.matches(f"{repo.name}/{rel}") for p in lang.include):
                    continue
                if any(p.matches(rel) for p in excludes):
                    continue
                files[path] = SourceFile(path, lang.name)
    return sorted(files.values(), key=lambda f: f.path)
