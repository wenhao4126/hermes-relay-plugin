from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


SCHEMA_VERSION = 2
RULES_VERSION = "relay-task-gate-v3"
TASK_KEYS_V1 = {
    "schema_version", "task_id", "revision", "goal", "scope", "behavior",
    "acceptance", "evidence_policy", "docs", "working_directory",
}
TASK_KEYS_V2 = TASK_KEYS_V1 | {"required_doc_ids"}
BEHAVIOR_KEYS = {"inputs", "outputs", "errors", "compatibility"}
DOC_KEYS = {"source", "version", "sha256"}
DOC_REF_KEYS = {"id", "project_version", "content_sha256"}
EVIDENCE_KINDS = {"tests", "build", "lint", "review", "ocr_review"}


class TaskContractError(ValueError):
    code = "TASK_PACKAGE_INVALID"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TaskContractError(message)


def _strings(value, field: str, *, nonempty: bool = True) -> None:
    _require(type(value) is list, f"{field} 必须是字符串数组")
    if nonempty:
        _require(bool(value), f"{field} 不能为空")
    for index, item in enumerate(value):
        _require(type(item) is str and bool(item.strip()),
                 f"{field}[{index}] 必须是非空字符串")


def _json_primitives(value, path: str = "任务包") -> None:
    if type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str, f"{path} 对象键必须是字符串")
            _json_primitives(item, f"{path}.{key}")
    elif type(value) is list:
        for index, item in enumerate(value):
            _json_primitives(item, f"{path}[{index}]")
    else:
        _require(type(value) in {str, int, bool},
                 f"{path} 只接受 string、int 或 bool")


def validate_task(value):
    _require(type(value) is dict, "任务包顶层必须是对象")
    _json_primitives(value)
    version = value.get("schema_version")
    _require(type(version) is int and version in {1, SCHEMA_VERSION},
             "schema_version 必须是整数 1 或 2")
    expected_keys = TASK_KEYS_V1 if version == 1 else TASK_KEYS_V2
    _require(set(value) == expected_keys, "任务包字段缺失或包含未知字段")
    _require(type(value["task_id"]) is str and
             re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value["task_id"]) is not None,
             "task_id 格式无效")
    _require(type(value["revision"]) is int and value["revision"] >= 1,
             "revision 必须是大于零的整数")
    _require(type(value["goal"]) is str and bool(value["goal"].strip()),
             "goal 必须是非空字符串")

    scope = value["scope"]
    _require(type(scope) is dict and set(scope) == {"allowed", "forbidden"},
             "scope 必须只包含 allowed 和 forbidden")
    _strings(scope["allowed"], "scope.allowed")
    _strings(scope["forbidden"], "scope.forbidden", nonempty=False)

    behavior = value["behavior"]
    _require(type(behavior) is dict and set(behavior) == BEHAVIOR_KEYS,
             "behavior 必须包含 inputs、outputs、errors、compatibility")
    for key in sorted(BEHAVIOR_KEYS):
        _strings(behavior[key], f"behavior.{key}")
    _strings(value["acceptance"], "acceptance")
    if version == 1:
        _strings(value["evidence_policy"], "evidence_policy")
    else:
        policy = value["evidence_policy"]
        policy_keys = {"review", "final", "no_external_docs_reason"}
        _require(type(policy) is dict and set(policy) == policy_keys,
                 "v2 evidence_policy 必须包含 review、final、no_external_docs_reason")
        for stage in ("review", "final"):
            kinds = policy[stage]
            _require(type(kinds) is list and all(type(kind) is str for kind in kinds),
                     f"evidence_policy.{stage} 必须是字符串数组")
            _require(len(kinds) == len(set(kinds)),
                     f"evidence_policy.{stage} 必须使用唯一证据 kind")
            _require(all(kind in EVIDENCE_KINDS for kind in kinds),
                     f"evidence_policy.{stage} 包含未知证据 kind")
        _require(not set(policy["review"]) & {"review", "ocr_review"},
                 "review 阶段不能要求 review/ocr_review 本身")
        _require("review" in policy["final"] and
                 set(policy["review"]).issubset(policy["final"]),
                 "final 阶段必须包含独立 review 和 review 阶段必需证据")
        _require(type(policy["no_external_docs_reason"]) is str,
                 "evidence_policy.no_external_docs_reason 必须是字符串")

    docs = value["docs"]
    _require(type(docs) is list, "docs 必须是数组")
    if version == 1:
        for index, doc in enumerate(docs):
            _require(type(doc) is dict and set(doc) == DOC_KEYS,
                     f"docs[{index}] 必须只包含 source、version、sha256")
            for key in ("source", "version"):
                _require(type(doc[key]) is str and bool(doc[key].strip()),
                         f"docs[{index}].{key} 必须是非空字符串")
            _require(type(doc["sha256"]) is str and
                     re.fullmatch(r"[a-f0-9]{64}", doc["sha256"]) is not None,
                     f"docs[{index}].sha256 必须是小写 SHA-256")
    else:
        ids = []
        for index, doc in enumerate(docs):
            _require(type(doc) is dict and set(doc) == DOC_REF_KEYS,
                     f"docs[{index}] 必须只包含 id、project_version、content_sha256")
            _require(type(doc["id"]) is str and
                     re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", doc["id"]) is not None,
                     f"docs[{index}].id 格式无效")
            _require(type(doc["project_version"]) is str and bool(doc["project_version"].strip()),
                     f"docs[{index}].project_version 必须是非空字符串")
            _require(type(doc["content_sha256"]) is str and
                     re.fullmatch(r"[a-f0-9]{64}", doc["content_sha256"]) is not None,
                     f"docs[{index}].content_sha256 必须是小写 SHA-256")
            ids.append(doc["id"])
        _require(len(ids) == len(set(ids)), "docs 中的资料 ID 不能重复")
        required_ids = value["required_doc_ids"]
        _strings(required_ids, "required_doc_ids", nonempty=False)
        _require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", doc_id)
                     for doc_id in required_ids), "required_doc_ids 包含非法 ID")
        _require(len(required_ids) == len(set(required_ids)), "required_doc_ids 不能重复")
        _require(set(required_ids).issubset(ids), "required_doc_ids 必须出现在 docs 引用中")
        if not required_ids:
            _require(bool(value["evidence_policy"]["no_external_docs_reason"].strip()),
                     "无必需外部资料时必须说明原因")

    cwd = value["working_directory"]
    _require(type(cwd) is str and bool(cwd), "working_directory 必须是绝对目录字符串")
    path = Path(cwd)
    _require(path.is_absolute(), "working_directory 必须是绝对路径")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TaskContractError("working_directory 必须存在") from exc
    _require(resolved.is_dir() and str(resolved) == cwd,
             "working_directory 必须是存在且规范化的真实目录")
    return value


def canonical_json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TaskContractError("任务包不能编码为规范 JSON") from exc


def canonical_digest(value) -> str:
    task = validate_task(value)
    bound = {"rules_version": RULES_VERSION, "task_package": task}
    return hashlib.sha256(canonical_json(bound).encode("utf-8")).hexdigest()


def docs_digest(value) -> str:
    task = validate_task(value)
    return hashlib.sha256(canonical_json(task["docs"]).encode("utf-8")).hexdigest()
