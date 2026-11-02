from pathlib import Path

from conftest import SAMPLE_GO
from godoc_agent.go_source import discover_go_files, extract_facts


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


def test_discover_skips_tests_vendor_and_hidden_dirs(tmp_path: Path):
    for rel in ["main.go", "pkg/a.go", "pkg/a_test.go", "vendor/x/x.go", ".cache/y.go", "pkg/testdata/z.go"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("package x\n", encoding="utf-8")

    found = [p.relative_to(tmp_path).as_posix() for p in discover_go_files(tmp_path)]
    with_tests = [p.relative_to(tmp_path).as_posix() for p in discover_go_files(tmp_path, include_tests=True)]

    assert found == ["main.go", "pkg/a.go"]
    assert with_tests == ["main.go", "pkg/a.go", "pkg/a_test.go"]
