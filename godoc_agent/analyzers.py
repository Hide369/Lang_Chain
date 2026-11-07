"""言語名から、その言語のソース解析処理を引く。

対応言語を増やすときは、<言語>_source.py に extract_facts を作り、ここに1行足す。
targets.toml の [[languages]] の name にはここのキーを書く。
"""

from collections.abc import Callable

from . import csharp_source, go_source, typescript_source
from .source_common import SourceFacts

ANALYZERS: dict[str, Callable[[str], SourceFacts]] = {
    "go": go_source.extract_facts,
    "csharp": csharp_source.extract_facts,
    "typescript": typescript_source.extract_facts,
}


def extract_facts(language: str, source: str) -> SourceFacts:
    try:
        analyzer = ANALYZERS[language]
    except KeyError:
        raise ValueError(f"対応していない言語です: {language!r}（対応: {', '.join(ANALYZERS)}）") from None
    return analyzer(source)
