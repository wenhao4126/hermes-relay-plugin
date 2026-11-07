"""把既有本地门禁封装为三个受原主控授权的模型工具。"""

from agent import secret_scope

from .gate_backend import doc_registry, gates
from .gate_backend.task_contract import validate_task


_AUTHORITY_ERRORS = frozenset({
    "not_owner", "epoch_changed", "closed", "paused", "state_invalid", "state_io_error",
})
_DOCUMENT_FIELDS = (
    "id", "question", "library_id", "project_version", "source_url", "excerpt",
    "conclusion", "limitations", "retrieved_at", "status",
)
_ADJUDICATION_FIELDS = frozenset({
    "task", "jev_model", "reviewer", "reason", "evidence_ref", "risk_level",
    "authorization_confirmed",
})
_DOC_LIMITS = {
    "id": 96, "question": 512, "library_id": 256, "project_version": 128,
    "source_url": 2048, "excerpt": 65536, "conclusion": 4096, "limitations": 4096,
    "retrieved_at": 64, "status": 16, "content_sha256": 64,
}
_SAFE_GATE_ERRORS = frozenset({
    "DOC_RECORD_INVALID", "DOC_REQUIRED_MISSING", "DOC_CONFLICT", "DOC_VERSION_MISMATCH",
    "DOC_DIGEST_MISMATCH", "DOC_ID_CONFLICT", "GATE_RECORD_INVALID", "GATE_STATE_INVALID",
    "TASK_PACKAGE_INVALID", "TASK_GATE_REQUIRED", "TASK_GATE_CONFIG_INVALID",
    "TASK_GATE_RESPONSE_INVALID", "TASK_GATE_ADJUDICATION_INVALID",
    "TASK_GATE_ADJUDICATION_NOT_ALLOWED",
})


def _failure(error):
    return {"ok": False, "error": error}


def _schema(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _safe_operation_error(exc, fallback):
    code = getattr(exc, "code", None)
    return code if type(code) is str and code in _SAFE_GATE_ERRORS else fallback


def _utf8_text(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_bounded_text(value, limit):
    return (
        type(value) is str
        and 0 < len(value) <= limit
        and bool(value.strip())
        and "\x00" not in value
        and _utf8_text(value)
    )


def _failure_json(error):
    import json

    return json.dumps(error if type(error) is dict else _failure(error), ensure_ascii=False)


def _authorize(authority_context):
    try:
        value = authority_context()
    except Exception:
        return None, None, "state_io_error"
    if type(value) is not tuple or len(value) != 3:
        return None, None, "state_invalid"
    epoch, result, check = value
    if epoch is None:
        if type(result) is dict and result.get("ok") is False and type(result.get("error")) is str:
            return None, None, result
        return None, None, "state_invalid"
    if type(epoch) is not str or not epoch or type(result) is not dict or not callable(check):
        return None, None, "state_invalid"
    error = _check_authority(check)
    if error is not None:
        return None, None, error
    return epoch, check, None


def _check_authority(check):
    try:
        value = check()
    except Exception:
        return "not_owner"
    if value is None:
        return None
    if type(value) is str and value in _AUTHORITY_ERRORS:
        return value
    return "not_owner"


def register_gate_tools(ctx, data_dir, authority_context):
    """注册资料、评估和显式裁决工具；身份与代次只来自入口闭包。"""
    document_properties = {
        field: {"type": "string", "minLength": 1, "maxLength": _DOC_LIMITS[field]}
        for field in _DOCUMENT_FIELDS
    }
    document_properties["id"]["pattern"] = "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$"
    document_properties["status"]["enum"] = ["verified", "missing", "conflict"]
    document_properties["content_sha256"] = {
        "type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[a-f0-9]{64}$",
    }
    document_schema = {
        "type": "object",
        "properties": document_properties,
        "required": list(_DOCUMENT_FIELDS),
        "additionalProperties": False,
    }
    task_schema = {"type": "object"}
    properties = {"task": task_schema, "jev_model": {"type": "string", "minLength": 1, "maxLength": 128}}
    adjudication_properties = {
        "task": task_schema,
        "jev_model": {"type": "string", "minLength": 1, "maxLength": 128},
        "reviewer": {"type": "string", "minLength": 1, "maxLength": 256},
        "reason": {"type": "string", "minLength": 1, "maxLength": 4096},
        "evidence_ref": {"type": "string", "minLength": 1, "maxLength": 1024},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "authorization_confirmed": {"type": "boolean", "const": True},
    }

    def record_document(args: dict, **_kwargs) -> str:
        import json

        if type(args) is not dict or set(args) != {"document"}:
            return json.dumps(_failure("invalid_input"), ensure_ascii=False)
        document = args["document"]
        if (
            type(document) is not dict
            or (set(document) != set(_DOCUMENT_FIELDS)
                and set(document) != set(_DOCUMENT_FIELDS) | {"content_sha256"})
            or any(type(value) is not str for value in document.values())
            or any(
                not document.get(field, "").strip()
                for field in ("id", "question", "library_id", "project_version", "source_url", "conclusion")
            )
            or any(
                len(value) > _DOC_LIMITS[field] or "\x00" in value
                or not _utf8_text(value)
                for field, value in document.items()
            )
            or document.get("status") not in {"verified", "missing", "conflict"}
            or (document.get("status") == "verified" and not document.get("excerpt", "").strip())
            or (document.get("status") in {"missing", "conflict"} and not document.get("limitations", "").strip())
        ):
            return _failure_json("invalid_input")
        _epoch, check, error = _authorize(authority_context)
        if error:
            return _failure_json(error)
        try:
            saved = doc_registry.record_doc(document, data_dir)
        except Exception as exc:
            error = _check_authority(check)
            if error:
                return _failure_json(error)
            return _failure_json(_safe_operation_error(exc, "DOC_RECORD_INVALID"))
        error = _check_authority(check)
        if error:
            return _failure_json(error)
        return json.dumps({"ok": True, "document": saved}, ensure_ascii=False)

    def evaluate(args: dict, **_kwargs) -> str:
        import json

        if (
            type(args) is not dict or set(args) != {"task", "jev_model"}
            or type(args["task"]) is not dict
            or type(args["jev_model"]) is not str or not args["jev_model"].strip()
            or len(args["jev_model"]) > 128
        ):
            return _failure_json("invalid_input")
        _epoch, check, error = _authorize(authority_context)
        if error:
            return _failure_json(error)
        try:
            validate_task(args["task"])
        except Exception as exc:
            return _failure_json(_safe_operation_error(exc, "TASK_PACKAGE_INVALID"))
        try:
            api_key = secret_scope.get_secret("TYPESAFE_API_KEY")
        except Exception:
            return _failure_json("api_key_unavailable")
        if type(api_key) is not str or not api_key.strip():
            return _failure_json("api_key_unavailable")
        try:
            result = gates.evaluate_once(args["task"], args["jev_model"], api_key, data_dir)
        except Exception as exc:
            error = _check_authority(check)
            if error:
                return _failure_json(error)
            return _failure_json(_safe_operation_error(exc, "TASK_GATE_REQUIRED"))
        error = _check_authority(check)
        if error:
            return _failure_json(error)
        return json.dumps({"ok": True, "evaluation": result}, ensure_ascii=False)

    def adjudicate(args: dict, **_kwargs) -> str:
        import json

        if (
            type(args) is not dict or set(args) != _ADJUDICATION_FIELDS
            or type(args["task"]) is not dict
            or not _valid_bounded_text(args["jev_model"], 128)
            or not _valid_bounded_text(args["reviewer"], 256)
            or not _valid_bounded_text(args["reason"], 4096)
            or not _valid_bounded_text(args["evidence_ref"], 1024)
            or type(args["risk_level"]) is not str or args["risk_level"] not in {"low", "medium", "high"}
            or args["authorization_confirmed"] is not True
        ):
            return _failure_json("invalid_input")
        _epoch, check, error = _authorize(authority_context)
        if error:
            return _failure_json(error)
        try:
            validate_task(args["task"])
        except Exception as exc:
            return _failure_json(_safe_operation_error(exc, "TASK_PACKAGE_INVALID"))
        try:
            result = gates.record_adjudication(
                args["task"], args["jev_model"], data_dir,
                reviewer=args["reviewer"], reason=args["reason"],
                evidence_ref=args["evidence_ref"], risk_level=args["risk_level"],
                authorization_confirmed=args["authorization_confirmed"],
            )
        except Exception as exc:
            error = _check_authority(check)
            if error:
                return _failure_json(error)
            return _failure_json(_safe_operation_error(exc, "TASK_GATE_ADJUDICATION_INVALID"))
        error = _check_authority(check)
        if error:
            return _failure_json(error)
        return json.dumps({"ok": True, "adjudication": result}, ensure_ascii=False)

    tools = (
        (
            "relay_record_document",
            "登记供任务门禁核对的版本化资料记录。",
            {"document": document_schema}, ["document"], record_document,
        ),
        (
            "relay_evaluate_task",
            "使用当前profile的已配置TypeSafe密钥评估完整任务包。",
            properties, ["task", "jev_model"], evaluate,
        ),
        (
            "relay_adjudicate_task",
            "仅为合格ESCALATE记录主控显式裁决。",
            adjudication_properties, sorted(_ADJUDICATION_FIELDS), adjudicate,
        ),
    )
    for name, description, fields, required, handler in tools:
        result = ctx.register_tool(
            name=name,
            toolset="relay",
            schema=_schema(name, description, fields, required),
            handler=handler,
            description=description,
        )
        if result is None:
            raise RuntimeError("tool_registration_failed")

