from godoc_agent.csharp_source import extract_facts
from godoc_agent.source_common import first_code_difference

SAMPLE_CS = """using System;
using System.Threading.Tasks;
using IO = System.IO;

namespace MyApp.Api.Controllers;

// public class Commented はコメントなので抽出しない
[ApiController]
[Route("api/[controller]")]
public sealed class UserController : ControllerBase
{
    private readonly IUserService _service;
    public const string Name = "public class InString";

    public UserController(IUserService service) { _service = service; }

    [HttpGet("{id}")]
    public async Task<ActionResult<User>> GetAsync(string id) => await _service.FindAsync(id);

    public string Title { get; set; } = "";

    internal void Helper() { }

    private static bool Validate(string id) { return id != ""; }
}

public record UserDto(string Id, string Name);

internal interface IUserService
{
    Task<User> FindAsync(string id);
}

public enum Role { Admin, User }
"""


def test_extracts_namespace_usings_types_and_public_members():
    facts = extract_facts(SAMPLE_CS)

    assert facts.language == "C#"
    assert facts.package == "MyApp.Api.Controllers"
    assert "namespace: MyApp.Api.Controllers" in facts.to_prompt()
    assert facts.imports == ["System", "System.IO", "System.Threading.Tasks"]
    assert facts.types == ["IUserService", "Role", "UserController", "UserDto"]
    assert facts.functions == ["UserController", "GetAsync", "Helper", "Validate"]
    assert facts.exported == ["GetAsync", "Name", "Role", "Title", "UserController", "UserDto"]


def test_comments_and_strings_do_not_produce_identifiers():
    facts = extract_facts(SAMPLE_CS)

    assert "Commented" not in facts.exported
    assert "InString" not in facts.types


def test_block_namespace_and_global_namespace():
    assert extract_facts("namespace A.B\n{\n    public class C { }\n}\n").package == "A.B"
    assert extract_facts("public class C { }\n").package == "(global)"


def test_adding_comments_is_not_a_code_change():
    commented = SAMPLE_CS.replace(
        "    public UserController(", "    /// <summary>コンストラクター。</summary>\n    public UserController("
    )

    assert first_code_difference(SAMPLE_CS, commented) is None
    assert first_code_difference(SAMPLE_CS, SAMPLE_CS.replace('id != ""', 'id == ""')) == 24
