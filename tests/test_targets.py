from pathlib import Path

import pytest

from godoc_agent.targets import compile_pattern, discover_source_files, load_targets

REPO_ROOT = Path(__file__).resolve().parent.parent


def write_files(root: Path, rel_paths: list[str]) -> None:
    for rel in rel_paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("package x\n", encoding="utf-8")


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "targets.toml"
    path.write_text(body, encoding="utf-8")
    return path


def found(repo: Path, config_path: Path) -> list[tuple[str, str]]:
    return [(f.path.relative_to(repo).as_posix(), f.language) for f in discover_source_files(repo, load_targets(config_path))]


EXAMPLE = REPO_ROOT / "targets.example.toml"


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/?*_api/pkg/handler/**", "user_api/pkg/handler/user.go", True),
        ("**/?*_api/pkg/handler/**", "services/order_api/pkg/handler/a/b.go", True),
        ("**/?*_api/pkg/handler/**", "_api/pkg/handler/x.go", False),  # xx 部分が必要
        ("**/?*_api/pkg/handler/**", "user_api/internal/pkg/handler/x.go", False),
        ("**/?*_api/pkg/handler/**", "user_api/pkg/handler", False),  # フォルダと同名のファイル自体は含めない
        ("**/*_test.go", "a_test.go", True),
        ("**/*_test.go", "x/a_test.go", True),
        ("*.go", "x/a.go", False),  # * は / をまたがない
        ("src/[!.]*.ts", "src/a.ts", True),
        ("src/[!.]*.ts", "src/.a.ts", False),
    ],
)
def test_glob_patterns(pattern, path, expected):
    assert compile_pattern(pattern, "test").matches(path) is expected


def test_example_config_targets_only_api_handler_dirs(tmp_path: Path):
    write_files(tmp_path, [
        "user_api/pkg/handler/user.go",
        "user_api/pkg/handler/admin/role.go",
        "services/order_api/pkg/handler/order.go",
        "main.go",
        "user_api/main.go",
        "user_api/pkg/model/user.go",
        "user_api/internal/pkg/handler/x.go",
        "_api/pkg/handler/no_prefix.go",
        "user_api_v2/pkg/handler/other.go",
    ])

    assert [p for p, _ in found(tmp_path, EXAMPLE)] == [
        "services/order_api/pkg/handler/order.go",
        "user_api/pkg/handler/admin/role.go",
        "user_api/pkg/handler/user.go",
    ]


def test_example_config_accepts_api_dir_itself_as_repo(tmp_path: Path):
    repo = tmp_path / "user_api"
    write_files(repo, ["pkg/handler/user.go", "pkg/model/user.go"])

    assert found(repo, EXAMPLE) == [("pkg/handler/user.go", "go")]


def test_example_config_skips_tests_vendor_and_hidden_dirs(tmp_path: Path):
    handler = "user_api/pkg/handler"
    write_files(tmp_path, [
        f"{handler}/a.go",
        f"{handler}/a_test.go",
        f"{handler}/vendor/x.go",
        f"{handler}/.cache/y.go",
        f"{handler}/testdata/z.go",
    ])

    assert found(tmp_path, EXAMPLE) == [(f"{handler}/a.go", "go")]


def test_multiple_languages_are_assigned_by_extension(tmp_path: Path):
    config = write_config(tmp_path, """
exclude = ["**/generated/**"]

[[languages]]
name = "go"
extensions = [".go"]
include = ["**/handler/**"]

[[languages]]
name = "csharp"
extensions = [".cs"]
include = ["**/Controllers/**"]
exclude = ["**/*Tests.cs"]

[[languages]]
name = "typescript"
extensions = [".ts", ".tsx"]
include = ["web/src/**"]
""")
    repo = tmp_path / "repo"
    write_files(repo, [
        "backend/handler/a.go",
        "backend/handler/generated/gen.go",  # 共通の除外
        "Api/Controllers/UserController.cs",
        "Api/Controllers/UserControllerTests.cs",  # 言語ごとの除外
        "Api/Services/UserService.cs",  # include の外
        "web/src/user.ts",
        "web/src/App.tsx",
        "web/src/handler/x.go",  # .go なので Go の include で判定され、対象外
    ])

    assert found(repo, config) == [
        ("Api/Controllers/UserController.cs", "csharp"),
        ("backend/handler/a.go", "go"),
        ("web/src/App.tsx", "typescript"),
        ("web/src/handler/x.go", "go"),
        ("web/src/user.ts", "typescript"),
    ]


def test_disabled_language_is_ignored(tmp_path: Path):
    config = write_config(tmp_path, """
[[languages]]
name = "go"
extensions = [".go"]
include = ["**"]

[[languages]]
name = "csharp"
enabled = false
extensions = [".cs"]
include = ["**"]
""")
    repo = tmp_path / "repo"
    write_files(repo, ["a.go", "B.cs"])

    assert found(repo, config) == [("a.go", "go")]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[[languages]]\nname = 'rust'\nextensions = ['.rs']\ninclude = ['**']\n", "name"),
        ("[[languages]]\nname = 'go'\nextensions = ['go']\ninclude = ['**']\n", "extensions"),
        ("[[languages]]\nname = 'go'\nextensions = ['.go']\ninclude = []\n", "include"),
        ("[[languages]]\nname = 'go'\nextensions = ['.go']\n", "include"),
        ("[[languages]]\nname = 'go'\nextensions = ['.go']\ninclude = ['../x/**']\n", "include"),
        ("[[languages]]\nname = 'go'\nextensions = ['.go']\ninclude = ['/abs/**']\n", "include"),
        ("[[languages]]\nname = 'go'\nextensions = ['.go']\ninclude = ['a\\\\b']\n", "include"),
        ("[[languages]]\nname = 'go'\nextensions = ['.go']\ninclude = ['**']\nincludes = ['x']\n", "includes"),
        ("[[languages]]\nname = 'go'\nenabled = 'yes'\nextensions = ['.go']\ninclude = ['**']\n", "enabled"),
        ("[[languages]]\nname = 'go'\nenabled = false\nextensions = ['.go']\ninclude = ['**']\n", "有効"),
        ("exclude = ['**']\n", "languages"),
        ("excludes = []\n[[languages]]\nname = 'go'\nextensions = ['.go']\ninclude = ['**']\n", "excludes"),
        (
            "[[languages]]\nname = 'go'\nextensions = ['.go']\ninclude = ['**']\n"
            "[[languages]]\nname = 'go'\nextensions = ['.gox']\ninclude = ['**']\n",
            "同じ言語",
        ),
        (
            "[[languages]]\nname = 'csharp'\nextensions = ['.x']\ninclude = ['**']\n"
            "[[languages]]\nname = 'typescript'\nextensions = ['.x']\ninclude = ['**']\n",
            "拡張子 .x",
        ),
        ("[[languages]\n", "書式"),
    ],
)
def test_invalid_config_fails_with_clear_message(tmp_path: Path, body, message):
    path = write_config(tmp_path, body)

    with pytest.raises(ValueError, match=message) as e:
        load_targets(path)
    assert str(path) in str(e.value)  # どの設定ファイルの誤りか分かる


def test_missing_config_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="none.toml"):
        load_targets(tmp_path / "none.toml")
