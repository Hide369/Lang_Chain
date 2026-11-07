from godoc_agent.source_common import first_code_difference
from godoc_agent.typescript_source import extract_facts

SAMPLE_TS = """import { Request, Response } from "express";
import type { User } from './models/user';
import "reflect-metadata";
const legacy = require("legacy-lib");

// export function commented() はコメントなので抽出しない
const message = "export class InString";

export interface UserRepository {
  find(id: string): Promise<User | undefined>;
}

export type UserId = string;

export enum Role { Admin, User }

export class UserHandler {
  constructor(private readonly repo: UserRepository) {}

  async get(req: Request, res: Response): Promise<void> {
    res.json(await this.repo.find(req.params.id));
  }
}

export async function createHandler(repo: UserRepository): Promise<UserHandler> {
  return new UserHandler(repo);
}

export const toId = (value: string): UserId => value.trim();

function validate(id: string): boolean {
  return id !== "";
}

const internalHelper = async (x: number) => x * 2;

export { validate as isValidId, type User };
export default UserHandler;
"""


def test_extracts_imports_types_functions_and_exports():
    facts = extract_facts(SAMPLE_TS)

    assert facts.language == "TypeScript"
    assert facts.package is None
    assert "package" not in facts.to_prompt()  # パッケージ宣言のない言語は行ごと出さない
    assert facts.imports == ["./models/user", "express", "legacy-lib", "reflect-metadata"]
    assert facts.types == ["Role", "UserHandler", "UserId", "UserRepository"]
    assert facts.functions == ["createHandler", "validate", "toId", "internalHelper"]
    assert facts.exported == [
        "Role", "User", "UserHandler", "UserId", "UserRepository", "createHandler", "isValidId", "toId",
    ]


def test_comments_and_strings_do_not_produce_identifiers():
    facts = extract_facts(SAMPLE_TS)

    assert "commented" not in facts.exported
    assert "InString" not in facts.types


def test_adding_comments_is_not_a_code_change():
    commented = SAMPLE_TS.replace("export class UserHandler {", "/** ユーザーAPIのハンドラー。 */\nexport class UserHandler {")

    assert first_code_difference(SAMPLE_TS, commented) is None
    assert first_code_difference(SAMPLE_TS, SAMPLE_TS.replace("x * 2", "x * 3")) == 35
