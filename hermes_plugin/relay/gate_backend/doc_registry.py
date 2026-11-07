from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .task_contract import canonical_json


FIELDS = {
    "id", "question", "library_id", "project_version", "source_url", "excerpt",
    "conclusion", "limitations", "retrieved_at", "status",
}
STATUSES = {"verified", "missing", "conflict"}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


class DocRecordError(ValueError):
    code = "DOC_RECORD_INVALID"

    def __init__(self, message: str, code: str = "DOC_RECORD_INVALID"):
        super().__init__(message)
        self.code = code


def _fail(message: str, code: str = "DOC_RECORD_INVALID"):
    raise DocRecordError(message, code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 对象键重复")
        result[key] = value
    return result


def _strict_json(path: Path):
    def reject_constant(_value):
        raise ValueError("JSON 常量无效")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                      parse_constant=reject_constant)


def _validate_input(value):
    if type(value) is not dict or frozenset(value) not in {
            frozenset(FIELDS), frozenset(FIELDS | {"content_sha256"})}:
        _fail("资料记录字段缺失或包含未知字段")
    for key in FIELDS:
        if type(value[key]) is not str:
            _fail(f"{key} 必须是字符串")
    if ID_PATTERN.fullmatch(value["id"]) is None:
        _fail("资料 ID 格式无效")
    for key in ("question", "library_id", "project_version", "source_url", "conclusion"):
        if not value[key].strip():
            _fail(f"{key} 不能为空")
    if value["status"] not in STATUSES:
        _fail("status 必须是 verified、missing 或 conflict")
    if value["status"] == "verified" and not value["excerpt"].strip():
        _fail("verified 资料必须包含 excerpt")
    if value["status"] in {"missing", "conflict"} and not value["limitations"].strip():
        _fail("missing/conflict 资料必须说明 limitations")
    if not value["library_id"].startswith("/"):
        _fail("library_id 必须是 Context7 库标识")
    parsed_url = urlsplit(value["source_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or parsed_url.username or parsed_url.password:
        _fail("source_url 必须是无凭据的 HTTP(S) 来源链接")
    try:
        timestamp = datetime.fromisoformat(value["retrieved_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise DocRecordError("retrieved_at 必须是 ISO-8601 日期") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        _fail("retrieved_at 必须包含时区")
    if "content_sha256" in value and type(value["content_sha256"]) is not str:
        _fail("content_sha256 只能由程序生成")
    return {key: value[key] for key in FIELDS}


def content_digest(value) -> str:
    data = _validate_input({key: item for key, item in value.items() if key != "content_sha256"})
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def _records_dir(state_dir: Path, *, create: bool):
    state = Path(state_dir)
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        _fail("Relay state 必须是普通目录")
    directory = state / "doc_records"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        _fail("资料目录必须是普通目录")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    if directory.exists() and stat.S_IMODE(directory.stat().st_mode) & 0o077:
        _fail("资料目录权限过宽")
    return directory


def _atomic_write(path: Path, data: bytes):
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def record_doc(value, state_dir):
    data = _validate_input(value)
    digest = hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
    supplied_digest = value.get("content_sha256")
    if supplied_digest is not None and supplied_digest != digest:
        _fail("content_sha256 与资料内容不匹配", "DOC_DIGEST_MISMATCH")
    directory = _records_dir(Path(state_dir), create=True)
    path = directory / f"{data['id']}.json"
    lock_path = directory / f"{data['id']}.lock"
    if lock_path.is_symlink():
        _fail("资料锁不能是符号链接")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise DocRecordError("无法安全打开资料锁") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("资料锁必须是独立普通文件")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists() or path.is_symlink():
                _fail("资料 ID 已存在，拒绝覆盖", "DOC_ID_CONFLICT")
            record = {**data, "content_sha256": digest}
            _atomic_write(path, (canonical_json(record) + "\n").encode("utf-8"))
            return record
    finally:
        os.close(fd)


def load_doc(doc_id, state_dir):
    if type(doc_id) is not str or ID_PATTERN.fullmatch(doc_id) is None:
        _fail("资料 ID 格式无效")
    directory = _records_dir(Path(state_dir), create=False)
    path = directory / f"{doc_id}.json"
    if path.is_symlink() or not path.is_file():
        _fail(f"必需资料不存在：{doc_id}", "DOC_REQUIRED_MISSING")
    try:
        record = _strict_json(path)
    except Exception as exc:
        raise DocRecordError("资料记录无法安全解析", "DOC_RECORD_INVALID") from exc
    data = _validate_input(record)
    calculated = hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
    if record.get("content_sha256") != calculated:
        _fail("资料记录摘要失效", "DOC_DIGEST_MISMATCH")
    if record["id"] != doc_id:
        _fail("资料记录 ID 与文件名不匹配")
    return record


def load_required_docs(task, state_dir):
    if task.get("schema_version") != 2:
        return []
    refs = {item["id"]: item for item in task["docs"]}
    result = []
    for doc_id in task["required_doc_ids"]:
        ref = refs.get(doc_id)
        if ref is None:
            _fail(f"任务未引用必需资料：{doc_id}", "DOC_REQUIRED_MISSING")
        record = load_doc(doc_id, state_dir)
        if record["status"] != "verified":
            code = "DOC_REQUIRED_MISSING" if record["status"] == "missing" else "DOC_CONFLICT"
            _fail(f"必需资料状态不可用：{doc_id}", code)
        if record["project_version"] != ref["project_version"]:
            _fail(f"必需资料项目版本不匹配：{doc_id}", "DOC_VERSION_MISMATCH")
        if record["content_sha256"] != ref["content_sha256"]:
            _fail(f"必需资料摘要不匹配：{doc_id}", "DOC_DIGEST_MISMATCH")
        result.append({key: record[key] for key in (
            "id", "question", "library_id", "project_version", "source_url",
            "excerpt", "conclusion", "limitations", "content_sha256")})
    return result
