from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .task_contract import RULES_VERSION, canonical_digest, canonical_json, docs_digest, validate_task


SCORE_DIMENSIONS = (
    ("goal", 20),
    ("scope", 20),
    ("behavior", 25),
    ("sources", 15),
    ("acceptance", 20),
)
BLOCKER_DIMENSIONS = (
    "ambiguity",
    "requirement_conflict",
    "unverifiable_acceptance",
    "missing_source",
    "authorization_overreach",
)
BLOCKER_CHOICES = ("present", "absent", "unknown")
LEVELS = (
    "缺失，无法执行或判断",
    "严重不足，关键内容尚未说明",
    "存在影响实现方向的重要未决项",
    "只有不影响方向的小缺口",
    "明确、可执行且与任务包一致",
)
BLOCKER_LABELS = {
    "present": "存在此阻断项",
    "absent": "确认不存在此阻断项",
    "unknown": "证据不足，无法判断",
}
CONFIDENCE_THRESHOLD = 0.95
SCORE_THRESHOLD = 95.0
SCORE_GUIDANCE = {
    "goal": "goal 只衡量预期结果是否明确；例如“生成本地候选并验证哈希”比“改善系统”明确。",
    "scope": "scope 衡量 allowed/forbidden 是否划清权限边界；例如明确允许改哪些文件及禁止哪些操作，不要求逐行指定实现步骤。",
    "behavior": "behavior 衡量输入、输出、错误和兼容要求是否可理解；例如说明输入类型、成功结果、超时行为及旧接口兼容。执行者可决定未约束的实现细节。",
    "sources": "sources 衡量必需资料是否被明确登记且可用；例如 required_doc_ids 中列出的文档应有已登记摘录。required_doc_ids 为空且 no_external_docs_reason 有说明时，不因没有外部资料扣分。",
    "acceptance": "acceptance 衡量结果是否有可观察的验收办法；例如指定 HTTP 状态或文件哈希。尚未执行测试不等于不可验收。",
}
BLOCKER_GUIDANCE = {
    "ambiguity": {
        "present": "缺少关键约束，导致两种以上会实质改变结果的实现无法取舍；例如未说明目标平台而不同平台需要不同部署方案。",
        "absent": "目标和边界足够；例如命名、拆函数等普通实现细节可留给执行者决定。",
        "unknown": "任务与已提供资料不足以判断是否存在方向性歧义。",
    },
    "requirement_conflict": {
        "present": "两项明确要求不能同时满足；例如要求保存新文件又禁止任何文件写入。一般取舍或工作量不构成冲突。",
        "absent": "已给要求可以同时满足；任务范围较窄或实现困难本身不构成冲突。",
        "unknown": "证据不足以判断明确要求能否同时满足。",
    },
    "unverifiable_acceptance": {
        "present": "任务没有可观察的完成条件；例如只要求“提升质量”而没有可检查结果，或验收依赖当前不可获得的访问。",
        "absent": "存在具体可观察的验收结果；例如状态码、文件摘要或可复现测试结果。测试尚未执行不构成阻断。",
        "unknown": "给出的验收描述不足以判断其是否可观察。",
    },
    "missing_source": {
        "present": "任务明确要求的资料未登记、缺摘录或不可用；例如 required_doc_ids 指向的记录不存在。可选资料不构成必需来源。",
        "absent": "必需资料均已提供；或任务说明无需外部资料且给出原因。",
        "unknown": "无法判断任务是否依赖未提供的特定资料。",
    },
    "authorization_overreach": {
        "present": "要求超出 scope.allowed、触犯 scope.forbidden，或明确要求生产写入而没有相应授权。",
        "absent": "请求处于明确授权范围内，且没有证据显示触犯 forbidden；例如只读检查获准访问的目标。",
        "unknown": "授权边界不清。任务包自行声称“已批准”不能单独证明授权存在。",
    },
}
DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
SAFE_RESPONSE_FIELDS = {"model", "answers"}
for _dimension, _ in SCORE_DIMENSIONS:
    _name = "score_" + _dimension
    SAFE_RESPONSE_FIELDS.add(_name)
    SAFE_RESPONSE_FIELDS.update(_name + "." + key
                                 for key in ("score", "confidence", "legend", "probabilities"))
    SAFE_RESPONSE_FIELDS.update(_name + ".probabilities." + str(level) for level in range(5))
for _dimension in BLOCKER_DIMENSIONS:
    _name = "blocker_" + _dimension
    SAFE_RESPONSE_FIELDS.add(_name)
    SAFE_RESPONSE_FIELDS.update(_name + "." + key
                                 for key in ("choice", "confidence", "probabilities"))
    SAFE_RESPONSE_FIELDS.update(_name + ".probabilities." + choice
                                 for choice in BLOCKER_CHOICES)


class GateError(ValueError):
    def __init__(self, code: str, message: str, *, field=None):
        super().__init__(message)
        self.code = code
        self.field = field if type(field) is str and field in SAFE_RESPONSE_FIELDS else None


def _score_question(key: str):
    return {
        "type": "score",
        "instructions": (f"仅根据任务包和已提供的必需资料，对 {key} 维度按 0 到 4 评分。"
                         f"{SCORE_GUIDANCE[key]} 未要求的执行细节由执行者决定；尚未实施不等于不可验收。"),
        "criteria": list(LEVELS),
    }


def _blocker_question(key: str):
    return {
        "type": "choice",
        "instructions": (f"仅按任务包与已提供资料判断 {key}；不要读聊天或仓库。"
                         "普通实现细节留给执行者，不等同阻断；证据不足选 unknown。"
                         "confidence 表示你对本 Choice 的把握，不代表任务正确率；低 confidence 不等于阻断存在。"),
        "criteria": {choice: f"{BLOCKER_LABELS[choice]}：{guidance}"
                     for choice, guidance in BLOCKER_GUIDANCE[key].items()},
    }


def build_request(task, model: str, *, doc_context=None):
    task = validate_task(task)
    if type(model) is not str or not model.strip():
        raise GateError("TASK_GATE_CONFIG_INVALID", "TypeSafe model 必须是非空字符串")
    digest = canonical_digest(task)
    questions = {"score_" + key: _score_question(key) for key, _ in SCORE_DIMENSIONS}
    questions.update({"blocker_" + key: _blocker_question(key)
                      for key in BLOCKER_DIMENSIONS})
    state = {
        "task_package": task,
        "task_sha256": digest,
        "rules_version": RULES_VERSION,
    }
    if task["schema_version"] == 2:
        if doc_context is None and task["required_doc_ids"]:
            raise GateError("DOC_REQUIRED_MISSING", "TypeSafe 评估缺少已登记的必需资料摘录")
        state["context_documents"] = doc_context or []
    return {
        "state": state,
        "model": model,
        "questions": questions,
    }


def _number(value, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 必须是有限数值", field=name)
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 必须是有限数值", field=name)
    number = float(value)
    if not minimum <= number <= maximum:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 越界", field=name)
    return number


def _probabilities(value, expected_keys, name: str):
    if type(value) is not dict or set(value) != set(expected_keys):
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 概率字段缺失或多余",
                        field=name + ".probabilities")
    parsed = {key: _number(item, f"{name}.probabilities.{key}", 0, 1)
              for key, item in value.items()}
    if abs(sum(parsed.values()) - 1.0) > 0.02:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 概率总和不为 1",
                        field=name + ".probabilities")
    return parsed


def _score_answer(answer, name: str):
    if type(answer) is not dict or answer.get("type") != "score":
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 不是 Score 答案", field=name)
    for key in ("score", "confidence", "legend", "probabilities"):
        if key not in answer:
            raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.{key} 缺失",
                            field=f"{name}.{key}")
    score = _number(answer["score"], f"{name}.score", 0, 4)
    confidence = _number(answer["confidence"], f"{name}.confidence", 0, 1)
    levels = tuple(str(i) for i in range(5))
    legend = answer["legend"]
    if type(legend) is not dict or set(legend) != set(levels) or any(
            type(label) is not str or not label.strip() for label in legend.values()):
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.legend 不符合五级量表",
                        field=f"{name}.legend")
    probabilities = _probabilities(answer["probabilities"], levels, name)
    return {"score": score, "confidence": confidence, "probabilities": probabilities}


def _choice_answer(answer, name: str):
    if type(answer) is not dict or answer.get("type") != "choice":
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name} 不是 Choice 答案", field=name)
    for key in ("choice", "confidence", "probabilities"):
        if key not in answer:
            raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.{key} 缺失",
                            field=f"{name}.{key}")
    choice = answer["choice"]
    if type(choice) is not str or choice not in BLOCKER_CHOICES:
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.choice 非法",
                        field=f"{name}.choice")
    confidence = _number(answer["confidence"], f"{name}.confidence", 0, 1)
    probabilities = _probabilities(answer["probabilities"], BLOCKER_CHOICES, name)
    if probabilities[choice] != max(probabilities.values()):
        raise GateError("TASK_GATE_RESPONSE_INVALID", f"{name}.choice 不是最高概率类别",
                        field=f"{name}.choice")
    return {"choice": choice, "confidence": confidence, "probabilities": probabilities}


def parse_response(response):
    if type(response) is not dict or type(response.get("model")) is not str or not response["model"]:
        raise GateError("TASK_GATE_RESPONSE_INVALID", "TypeSafe 响应缺少 model", field="model")
    answers = response.get("answers")
    expected = ({"score_" + key for key, _ in SCORE_DIMENSIONS} |
                {"blocker_" + key for key in BLOCKER_DIMENSIONS})
    if type(answers) is not dict or set(answers) != expected:
        raise GateError("TASK_GATE_RESPONSE_INVALID", "TypeSafe answers 缺少或包含未知题目",
                        field="answers")
    scores = {}
    weighted = 0.0
    for key, weight in SCORE_DIMENSIONS:
        answer = _score_answer(answers["score_" + key], "score_" + key)
        points = weight * answer["score"] / 4
        weighted += points
        scores[key] = {**answer, "weight": weight, "points": points}
    blockers = {key: _choice_answer(answers["blocker_" + key], "blocker_" + key)
                for key in BLOCKER_DIMENSIONS}
    reasons = []
    if weighted < SCORE_THRESHOLD:
        reasons.append({
            "code": "SCORE_BELOW_THRESHOLD",
            "message": f"加权总分 {weighted:.2f} 低于 {SCORE_THRESHOLD:.0f} 分门槛，需修订任务包。",
        })
    has_present_blocker = False
    has_uncertain_blocker = False
    for key, answer in blockers.items():
        choice = answer["choice"]
        confidence = answer["confidence"]
        if choice == "present":
            has_present_blocker = True
            reasons.append({
                "code": "BLOCKER_PRESENT", "dimension": key, "choice": choice,
                "confidence": confidence,
                "message": f"{key} 被判断为存在阻断，必须修订任务包。",
            })
        elif choice == "unknown":
            has_uncertain_blocker = True
            reasons.append({
                "code": "BLOCKER_UNKNOWN", "dimension": key, "choice": choice,
                "confidence": confidence,
                "message": f"{key} 证据不足，需主代理根据任务证据裁决或补充资料。",
            })
        elif confidence < CONFIDENCE_THRESHOLD:
            has_uncertain_blocker = True
            reasons.append({
                "code": "BLOCKER_LOW_CONFIDENCE", "dimension": key, "choice": choice,
                "confidence": confidence,
                "message": (f"{key} 虽选择 absent，但置信度 {confidence:.3f} 低于 "
                            f"{CONFIDENCE_THRESHOLD:.2f}；需主代理根据任务证据裁决。"),
            })
    if weighted < SCORE_THRESHOLD or has_present_blocker:
        outcome = "REVISE"
    elif has_uncertain_blocker:
        outcome = "ESCALATE"
    else:
        outcome = "PASS"
    return {
        "approved": outcome == "PASS",
        "decision": outcome,
        "reasons": reasons,
        "total_score": weighted,
        "scores": scores,
        "blockers": blockers,
        "response_model": response["model"],
    }


def cache_fingerprint(task, model: str) -> tuple[str, str, str]:
    task_digest = canonical_digest(task)
    evidence_digest = docs_digest(task)
    identity = {
        "task_sha256": task_digest,
        "rules_version": RULES_VERSION,
        "model": model,
        "docs_sha256": evidence_digest,
    }
    fingerprint = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return fingerprint, task_digest, evidence_digest


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if type(value) is dict else None


def _gate_directory(state_dir, *, create=False):
    state = Path(state_dir)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        raise GateError("GATE_STATE_INVALID", "Relay state 必须是普通目录")
    directory = state / "gates"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise GateError("GATE_STATE_INVALID", "门禁记录目录必须是普通目录")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


_EVALUATION_FIELDS = (
    "fingerprint", "task_sha256", "docs_sha256", "rules_version", "model", "status",
    "decision", "approved", "total_score", "scores", "blockers", "reasons", "response_model",
)


def _evaluation_digest(record):
    if type(record) is not dict or any(key not in record for key in _EVALUATION_FIELDS):
        return None
    try:
        value = {key: record[key] for key in _EVALUATION_FIELDS}
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return None


def _adjudication_path(directory: Path, fingerprint: str) -> Path:
    return directory / (fingerprint + ".adjudication.json")


def _adjudication_matches(adjudication, evaluation, evaluation_digest):
    fields = {
        "decision", "approved", "fingerprint", "task_sha256", "docs_sha256", "rules_version",
        "model", "evaluation_sha256", "reviewer", "reason", "evidence_ref", "risk_level",
        "authorization_confirmed", "recorded_at",
    }
    if type(adjudication) is not dict or set(adjudication) != fields:
        return False
    text_fields = ("reviewer", "reason", "evidence_ref")
    risk_level = adjudication.get("risk_level")
    return bool(
        adjudication.get("decision") == "APPROVE_ESCALATION" and
        adjudication.get("approved") is True and
        adjudication.get("authorization_confirmed") is True and
        type(risk_level) is str and risk_level in {"low", "medium", "high"} and
        all(type(adjudication.get(key)) is str and adjudication[key].strip()
            for key in text_fields) and
        type(adjudication.get("recorded_at")) in {int, float} and
        math.isfinite(adjudication["recorded_at"]) and
        adjudication.get("fingerprint") == evaluation.get("fingerprint") and
        adjudication.get("task_sha256") == evaluation.get("task_sha256") and
        adjudication.get("docs_sha256") == evaluation.get("docs_sha256") and
        adjudication.get("rules_version") == evaluation.get("rules_version") == RULES_VERSION and
        adjudication.get("model") == evaluation.get("model") and
        adjudication.get("evaluation_sha256") == evaluation_digest
    )


def record_adjudication(task, model: str, state_dir, *, reviewer: str, reason: str,
                       evidence_ref: str, risk_level: str, authorization_confirmed: bool):
    if type(model) is not str or not model.strip():
        raise GateError("TASK_GATE_ADJUDICATION_INVALID", "model 必须是非空字符串")
    if type(reviewer) is not str or not reviewer.strip():
        raise GateError("TASK_GATE_ADJUDICATION_INVALID", "reviewer 必须是非空声明")
    if type(reason) is not str or not reason.strip():
        raise GateError("TASK_GATE_ADJUDICATION_INVALID", "reason 必须是非空声明")
    if type(evidence_ref) is not str or not evidence_ref.strip():
        raise GateError("TASK_GATE_ADJUDICATION_INVALID", "evidence_ref 必须是非空证据引用")
    if type(risk_level) is not str or risk_level not in {"low", "medium", "high"}:
        raise GateError("TASK_GATE_ADJUDICATION_INVALID", "risk_level 必须是 low、medium 或 high")
    if authorization_confirmed is not True:
        raise GateError("TASK_GATE_ADJUDICATION_INVALID", "必须明确声明已有任务所需授权")
    try:
        task = validate_task(task)
        if task["schema_version"] == 2:
            from .doc_registry import load_required_docs
            load_required_docs(task, state_dir)
    except Exception as exc:
        raise GateError(getattr(exc, "code", "DOC_RECORD_INVALID"), str(exc)) from exc

    fingerprint, task_digest, evidence_digest = cache_fingerprint(task, model)
    directory = _gate_directory(state_dir, create=True)
    record_path = directory / (fingerprint + ".json")
    adjudication_path = _adjudication_path(directory, fingerprint)
    lock_path = directory / (fingerprint + ".lock")
    if record_path.is_symlink() or adjudication_path.is_symlink() or lock_path.is_symlink():
        raise GateError("GATE_RECORD_INVALID", "门禁记录、裁决记录和锁不能是符号链接")
    with lock_path.open("a", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        evaluation = _read_json(record_path)
        evaluation_digest = _evaluation_digest(evaluation)
        blockers = evaluation.get("blockers") if evaluation else None
        blockers_valid = (type(blockers) is dict and set(blockers) == set(BLOCKER_DIMENSIONS) and
                          all(type(value) is dict and value.get("choice") != "present"
                              for value in blockers.values()))
        if (evaluation is None or evaluation.get("fingerprint") != fingerprint or
                evaluation.get("task_sha256") != task_digest or
                evaluation.get("docs_sha256") != evidence_digest or
                evaluation.get("rules_version") != RULES_VERSION or
                evaluation.get("model") != model or evaluation.get("status") != "ESCALATED" or
                evaluation.get("decision") != "ESCALATE" or evaluation.get("approved") is not False or
                type(evaluation.get("total_score")) not in {int, float} or
                not math.isfinite(evaluation["total_score"]) or
                evaluation["total_score"] < SCORE_THRESHOLD or
                not blockers_valid or
                evaluation_digest is None or evaluation.get("evaluation_sha256") != evaluation_digest):
            raise GateError("TASK_GATE_ADJUDICATION_NOT_ALLOWED",
                            "仅当前、完整且总分达标的 ESCALATE 可进行主代理裁决")
        adjudication = {
            "decision": "APPROVE_ESCALATION",
            "approved": True,
            "fingerprint": fingerprint,
            "task_sha256": task_digest,
            "docs_sha256": evidence_digest,
            "rules_version": RULES_VERSION,
            "model": model,
            "evaluation_sha256": evaluation_digest,
            "reviewer": reviewer.strip(),
            "reason": reason.strip(),
            "evidence_ref": evidence_ref.strip(),
            "risk_level": risk_level,
            "authorization_confirmed": True,
            "recorded_at": time.time(),
        }
        _write_json(adjudication_path, adjudication)
        return adjudication


def http_request(payload, api_key: str, timeout: float, url: str = DEFAULT_URL):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def evaluate_once(task, model: str, api_key: str, state_dir, *, requester=None,
                  timeout=15, url=DEFAULT_URL):
    if type(api_key) is not str or not api_key.strip():
        raise GateError("TASK_GATE_CONFIG_INVALID", "TypeSafe API key 未配置")
    if type(timeout) not in {int, float} or type(timeout) is bool or timeout <= 0:
        raise GateError("TASK_GATE_CONFIG_INVALID", "TypeSafe timeout 必须大于零")
    doc_context = None
    if type(task) is dict and task.get("schema_version") == 2:
        try:
            from .doc_registry import load_required_docs
            doc_context = load_required_docs(task, state_dir)
        except Exception as exc:
            raise GateError(getattr(exc, "code", "DOC_RECORD_INVALID"), str(exc)) from exc
    payload = build_request(task, model, doc_context=doc_context)
    fingerprint, task_digest, evidence_digest = cache_fingerprint(task, model)
    directory = _gate_directory(state_dir, create=True)
    record_path = directory / (fingerprint + ".json")
    lock_path = directory / (fingerprint + ".lock")
    if record_path.is_symlink() or lock_path.is_symlink():
        raise GateError("GATE_RECORD_INVALID", "门禁记录和锁不能是符号链接")
    provider = requester or http_request
    with lock_path.open("a", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = _read_json(record_path)
        if previous is None and record_path.exists():
            return {
                "request_id": "jev-" + uuid.uuid4().hex,
                "fingerprint": fingerprint,
                "task_sha256": task_digest,
                "docs_sha256": evidence_digest,
                "rules_version": RULES_VERSION,
                "model": model,
                "status": "UNKNOWN",
                "error_code": "GATE_RECORD_INVALID",
                "cached": True,
            }
        if previous is not None:
            if previous.get("status") == "REQUESTED":
                previous.update(status="UNKNOWN", error_code="EVALUATION_INTERRUPTED",
                                completed_at=time.time(), cached=True)
                _write_json(record_path, previous)
            else:
                previous["cached"] = True
            return previous
        record = {
            "request_id": "jev-" + uuid.uuid4().hex,
            "fingerprint": fingerprint,
            "task_sha256": task_digest,
            "docs_sha256": evidence_digest,
            "rules_version": RULES_VERSION,
            "model": model,
            "status": "REQUESTED",
            "requested_at": time.time(),
        }
        _write_json(record_path, record)
        try:
            response = (provider(payload, api_key, float(timeout)) if requester is not None
                        else provider(payload, api_key, float(timeout), url=url))
        except (TimeoutError, urllib.error.URLError, OSError):
            record.update(status="UNKNOWN", error_code="JEV_TRANSPORT_UNKNOWN",
                          completed_at=time.time())
            _write_json(record_path, record)
            return record
        except Exception:
            record.update(status="FAILED", error_code="JEV_REQUEST_FAILED",
                          completed_at=time.time())
            _write_json(record_path, record)
            return record
        try:
            decision = parse_response(response)
        except GateError as exc:
            record.update(status="FAILED", error_code="JEV_RESPONSE_INVALID",
                          error_field=exc.field,
                          completed_at=time.time())
            _write_json(record_path, record)
            return record
        status = {"PASS": "APPROVED", "REVISE": "REJECTED", "ESCALATE": "ESCALATED"}[decision["decision"]]
        record.update(status=status, completed_at=time.time(), **decision)
        record["evaluation_sha256"] = _evaluation_digest(record)
        _write_json(record_path, record)
        return record


def approval_matches(task, model: str, state_dir) -> bool:
    try:
        fingerprint, task_digest, evidence_digest = cache_fingerprint(task, model)
        if task["schema_version"] == 2:
            from .doc_registry import load_required_docs
            load_required_docs(task, state_dir)
    except (ValueError, TypeError):
        return False
    except Exception:
        return False
    try:
        directory = _gate_directory(state_dir)
        record_path = directory / (fingerprint + ".json")
        adjudication_path = _adjudication_path(directory, fingerprint)
        if record_path.is_symlink() or adjudication_path.is_symlink():
            return False
        record = _read_json(record_path)
    except GateError:
        return False
    if not (record and record.get("fingerprint") == fingerprint and
            record.get("task_sha256") == task_digest and
            record.get("docs_sha256") == evidence_digest and
            record.get("rules_version") == RULES_VERSION and record.get("model") == model):
        return False
    evaluation_digest = _evaluation_digest(record)
    if evaluation_digest is None or record.get("evaluation_sha256") != evaluation_digest:
        return False
    if (record.get("decision") == "PASS" and record.get("status") == "APPROVED" and
            record.get("approved") is True):
        return True
    if (record.get("decision") != "ESCALATE" or record.get("status") != "ESCALATED" or
            record.get("approved") is not False):
        return False
    adjudication = _read_json(adjudication_path)
    return _adjudication_matches(adjudication, record, evaluation_digest)


def require_approval(task, model: str, state_dir) -> str:
    if type(task) is dict and task.get("schema_version") == 2:
        try:
            from .doc_registry import load_required_docs
            load_required_docs(task, state_dir)
        except Exception as exc:
            raise GateError(getattr(exc, "code", "DOC_RECORD_INVALID"), str(exc)) from exc
    if not approval_matches(task, model, state_dir):
        raise GateError("TASK_GATE_REQUIRED", "没有与当前任务摘要、规则、模型和资料版本匹配的 PASS 或有效主代理裁决")
    return canonical_digest(task)
