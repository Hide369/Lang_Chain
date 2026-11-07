from conftest import SAMPLE_GO
from godoc_agent.go_source import extract_facts
from godoc_agent.source_common import first_code_difference


def test_extract_facts_collects_package_imports_types_and_functions():
    facts = extract_facts(SAMPLE_GO)

    assert facts.package == "user"
    assert facts.imports == ["context", "errors"]
    assert facts.types == ["Repository", "User"]
    assert facts.functions == ["NewService", "Service.GetUser", "validate"]


def test_exported_excludes_private_identifiers_and_ignores_comments():
    facts = extract_facts(SAMPLE_GO)

    assert facts.exported == ["GetUser", "NewService", "Repository", "User"]
    assert "Commented" not in facts.functions


def test_extract_facts_handles_type_blocks_and_generics_without_struct_fields():
    source = """package box

import "fmt"

type (
\tID int
\tBox[T any] struct {
\t\tValue T
\t}
)

func Map[T, U any](b Box[T], f func(T) U) Box[U] { return Box[U]{Value: f(b.Value)} }
"""
    facts = extract_facts(source)

    assert facts.imports == ["fmt"]
    assert facts.types == ["Box", "ID"]
    assert facts.functions == ["Map"]


def test_comments_inside_string_literals_are_not_stripped():
    source = 'package h\n\nconst URL = "http://example.com" // 末尾コメント\n\nfunc Get() {}\n'

    assert extract_facts(source).functions == ["Get"]
    # 文字列中の "//" をコメントと誤認すると、文字列の変更を見逃す
    assert first_code_difference(source, source.replace("example.com", "evil.com")) == 3


def test_adding_comments_and_reformatting_whitespace_is_not_a_code_change():
    original = "package h\n\ntype User struct {\n\tID string\n\tName string\n}\n\nfunc Get(a,b int) int { return a+b }\n"
    commented = (
        "// Package h はハンドラー。\npackage h\n\n/*\nUser は利用者。\n*/\ntype User struct {\n"
        "\tID   string // 識別子\n\tName string\n}\n\n// Get は和を返す。\nfunc Get(a, b int) int { return a + b }\n"
    )

    assert first_code_difference(original, commented) is None


def test_code_changes_are_reported_with_original_line():
    original = "package h\n\nfunc Get() int {\n\treturn 1\n}\n"

    assert first_code_difference(original, original.replace("return 1", "return 2")) == 4
    assert first_code_difference(original, original.replace("return 1", "return1")) == 4
    # 改行は文末を表すため、行の結合もコードの変更として扱う
    assert first_code_difference(original, "package h\n\nfunc Get() int { return 1 }\n") == 3
    # 末尾の削除・追加も検出する
    assert first_code_difference(original, "package h\n") == 3
    assert first_code_difference(original, original + "\nfunc Extra() {}\n") == 5
