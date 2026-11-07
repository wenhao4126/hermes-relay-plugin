"""为当前 profile 已有代理提供受门禁约束的单次任务提交。"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .gate_backend.gates import require_approval
from .gate_backend.task_contract import TaskContractError, canonical_json, validate_task
from .herdr_reader import read_bound_agent


_SCHEMA_VERSION = 1
_STATE_NAME = "relay-dispatch.json"
_LOCK_NAME = "relay-dispatch.lock"
_MAX_STATE_BYTES = 8 * 1024 * 1024
_MAX_TASK_BYTES = 1024 * 1024
_ROOT_FIELDS = {"schema_version", "requests"}
_RECORD_FIELDS = {
    "request_id", "epoch", "binding", "instance_id", "task_id", "revision",
    "task_sha256", "jev_model", "owner_checks", "fingerprint", "state", "error",
}
_BINDING_FIELDS = {
    "herdr_session", "name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id",
}
_OWNER_CHECK_FIELDS = {"model", "permissions", "cwd", "input_ready"}
_AGENT_KINDS = {"codex", "opencode", "claude"}
_AGENT_FIELDS = {
    "name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id",
    "agent_status", "interactive_ready", "state_change_seq", "completion_seq",
}
_STATUSES = {"PREPARED", "SENDING", "SUBMITTED", "REFUSED", "UNKNOWN"}
_ERROR_CODES = {
    "invalid_input", "invalid_response", "closed", "paused", "epoch_changed", "not_owner", "not_bound",
    "request_conflict", "agent_busy_unknown", "agent_busy", "agent_already_watched",
    "submission_unknown", "state_invalid", "state_io_error", "observer_failed", "agent_not_ready",
    "identity_mismatch", "identity_unknown", "not_found", "unavailable", "state_unknown",
    "binding_conflict", "task_retired", "capacity_reached", "sequence_unknown", "sequence_regressed",
    "sequence_inconsistent", "inject_rejected", "delivery_unknown", "event_conflict", "shutdown_pending",
    "TASK_PACKAGE_INVALID", "TASK_GATE_REQUIRED", "TASK_GATE_CONFIG_INVALID", "TASK_GATE_RESPONSE_INVALID",
    "TASK_GATE_ADJUDICATION_INVALID", "TASK_GATE_ADJUDICATION_NOT_ALLOWED", "DOC_RECORD_INVALID",
    "DOC_REQUIRED_MISSING", "DOC_CONFLICT", "DOC_VERSION_MISMATCH", "DOC_DIGEST_MISMATCH",
    "DOC_ID_CONFLICT", "GATE_RECORD_INVALID", "GATE_STATE_INVALID",
}
_AUTHORITY_ERRORS = {"not_owner", "epoch_changed", "closed", "paused", "state_invalid", "state_io_error"}
_EPOCH_PATTERN = re.compile(r"epoch-[1-9][0-9]*\Z")
_REQUEST_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
_ERROR_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
class _StateFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _failure(error):
    return {"ok": False, "error": error}


def _utf8(value):
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_binding(binding):
    if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
        return False
    if any(
        not _utf8(value)
        or not value.strip()
        or any(character in value for character in ("\x00", "\r", "\n"))
        for value in binding.values()
    ):
        return False
    return (
        binding["agent_kind"] in _AGENT_KINDS
        and _NAME_PATTERN.fullmatch(binding["name"]) is not None
        and not binding["herdr_session"].startswith("-")
    )


def _valid_request_id(value):
    return (
        type(value) is str
        and _REQUEST_PATTERN.fullmatch(value) is not None
        and not value.startswith("-")
    )


def _valid_owner_checks(value):
    return (
        type(value) is dict
        and set(value) == _OWNER_CHECK_FIELDS
        and all(type(item) is bool for item in value.values())
        and all(value.values())
    )


def _empty_state():
    return {"schema_version": _SCHEMA_VERSION, "requests": []}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(value)


def _valid_record(record):
    if type(record) is not dict or set(record) != _RECORD_FIELDS:
        return False
    binding = record["binding"]
    if not _valid_request_id(record["request_id"]):
        return False
    if type(record["epoch"]) is not str or _EPOCH_PATTERN.fullmatch(record["epoch"]) is None:
        return False
    if not _valid_binding(binding):
        return False
    if type(record["instance_id"]) is not str or _DIGEST_PATTERN.fullmatch(record["instance_id"]) is None:
        return False
    if record["instance_id"] != hashlib.sha256(canonical_json(binding).encode("utf-8")).hexdigest():
        return False
    if type(record["task_id"]) is not str or _TASK_ID_PATTERN.fullmatch(record["task_id"]) is None:
        return False
    if type(record["revision"]) is not int or record["revision"] < 1:
        return False
    if any(type(record[field]) is not str or _DIGEST_PATTERN.fullmatch(record[field]) is None
           for field in ("task_sha256", "fingerprint")):
        return False
    if not _utf8(record["jev_model"]) or not record["jev_model"] or any(
        character in record["jev_model"] for character in ("\x00", "\r", "\n")
    ):
        return False
    checks = record["owner_checks"]
    if type(checks) is not dict or set(checks) != _OWNER_CHECK_FIELDS:
        return False
    if any(type(value) is not bool or value is not True for value in checks.values()):
        return False
    if type(record["state"]) is not str or record["state"] not in _STATUSES:
        return False
    error = record["error"]
    if error is not None and (
        type(error) is not str or _ERROR_PATTERN.fullmatch(error) is None or error not in _ERROR_CODES
    ):
        return False
    if record["state"] in {"PREPARED", "SENDING", "SUBMITTED"} and error is not None:
        return False
    if record["state"] in {"REFUSED", "UNKNOWN"} and error is None:
        return False
    return True


def _validate_state(state):
    if type(state) is not dict or set(state) != _ROOT_FIELDS:
        raise _StateFailure("state_invalid")
    if type(state["schema_version"]) is not int or state["schema_version"] != _SCHEMA_VERSION:
        raise _StateFailure("state_invalid")
    if type(state["requests"]) is not list:
        raise _StateFailure("state_invalid")
    seen = set()
    for record in state["requests"]:
        if not _valid_record(record) or record["request_id"] in seen:
            raise _StateFailure("state_invalid")
        seen.add(record["request_id"])


def _safe_error(value):
    if type(value) is str and _ERROR_PATTERN.fullmatch(value) is not None and value in _ERROR_CODES:
        return value
    return "invalid_response"


class Dispatcher:
    """以独立账本记录每个原主控派发请求，未知结果不自动重试。"""

    def __init__(self, data_dir, get_runtime, observer, *, reader=read_bound_agent, runner=None):
        if not isinstance(data_dir, Path):
            raise ValueError("invalid_input")
        path_text = str(data_dir)
        if not data_dir.is_absolute() or "\x00" in path_text or not _utf8(path_text):
            raise ValueError("invalid_input")
        if not callable(get_runtime) or not callable(reader):
            raise ValueError("invalid_input")
        if runner is not None and not callable(runner):
            raise ValueError("invalid_input")
        if not all(callable(getattr(observer, name, None)) for name in ("track", "untrack", "status")):
            raise ValueError("invalid_input")
        self.data_dir = data_dir
        self.state_path = data_dir / _STATE_NAME
        self.lock_path = data_dir / _LOCK_NAME
        self.get_runtime = get_runtime
        self.observer = observer
        self.reader = reader
        self.runner = runner
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    def close(self):
        with self._lifecycle_lock:
            self._closed = True

    def _is_closed(self):
        with self._lifecycle_lock:
            return self._closed

    def _active_error(self, epoch):
        if self._is_closed():
            return "closed"
        try:
            runtime = self.get_runtime()
            active_epoch = getattr(runtime, "active_epoch", None) if runtime is not None else None
        except Exception:
            return "epoch_changed"
        if runtime is None or active_epoch is None:
            return "paused"
        if type(active_epoch) is not str or active_epoch != epoch:
            return "epoch_changed"
        return None

    def _authority_error(self, epoch, authority_check):
        """每个派发阶段都重新核对本次原主控的磁盘身份。"""
        if not callable(authority_check):
            return "not_owner"
        if self._is_closed():
            return "closed"
        try:
            result = authority_check()
        except Exception:
            return "not_owner"
        if result is not None:
            if type(result) is str and result in _AUTHORITY_ERRORS:
                return result
            return "not_owner"
        return self._active_error(epoch)

    def _ensure_directory(self):
        try:
            if self.data_dir.is_symlink():
                raise _StateFailure("state_invalid")
            if not self.data_dir.exists():
                self.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = os.lstat(self.data_dir)
            if not stat.S_ISDIR(info.st_mode):
                raise _StateFailure("state_invalid")
        except _StateFailure:
            raise
        except OSError as exc:
            raise _StateFailure("state_io_error") from exc

    @staticmethod
    def _open_error(exc):
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
            return _StateFailure("state_invalid")
        return _StateFailure("state_io_error")

    @contextmanager
    def _locked(self):
        with self._lock:
            self._ensure_directory()
            try:
                metadata = os.lstat(self.lock_path)
            except FileNotFoundError:
                metadata = None
            except OSError as exc:
                raise self._open_error(exc) from None
            if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                raise _StateFailure("state_invalid")
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = -1
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
                lock_info = os.fstat(descriptor)
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                    raise _StateFailure("state_invalid")
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            except _StateFailure:
                raise
            except OSError as exc:
                raise self._open_error(exc) from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def _read_state_locked(self):
        try:
            metadata = os.lstat(self.state_path)
        except FileNotFoundError:
            return _empty_state()
        except OSError as exc:
            raise self._open_error(exc) from None
        if not stat.S_ISREG(metadata.st_mode):
            raise _StateFailure("state_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = -1
        try:
            descriptor = os.open(self.state_path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _StateFailure("state_invalid")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(_MAX_STATE_BYTES + 1)
            if len(raw) > _MAX_STATE_BYTES:
                raise _StateFailure("state_invalid")
            try:
                state = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
                raise _StateFailure("state_invalid") from None
            _validate_state(state)
            return state
        except _StateFailure:
            raise
        except OSError as exc:
            raise self._open_error(exc) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_state_locked(self, state):
        _validate_state(state)
        try:
            encoded = json.dumps(
                state, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise _StateFailure("state_invalid") from None
        if len(encoded) > _MAX_STATE_BYTES:
            raise _StateFailure("state_invalid")

        try:
            try:
                existing = os.lstat(self.state_path)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise _StateFailure("state_invalid")
            descriptor, temporary = tempfile.mkstemp(prefix=".relay-dispatch-", dir=str(self.data_dir))
        except _StateFailure:
            raise
        except OSError as exc:
            raise self._open_error(exc) from None

        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                existing = os.lstat(self.state_path)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise _StateFailure("state_invalid")
            os.replace(temporary, self.state_path)
            temporary = None
            directory_fd = os.open(self.data_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except _StateFailure:
            raise
        except OSError as exc:
            raise self._open_error(exc) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @contextmanager
    def _transaction(self):
        with self._locked():
            yield self._read_state_locked()

    @staticmethod
    def _find_request(state, request_id):
        return next((item for item in state["requests"] if item["request_id"] == request_id), None)

    @staticmethod
    def _public_request(record):
        status = "UNKNOWN" if record["state"] in {"PREPARED", "SENDING"} else record["state"]
        error = record["error"]
        if status == "UNKNOWN":
            error = "submission_unknown"
        return {
            "request_id": record["request_id"],
            "task_id": record["task_id"],
            "instance_id": record["instance_id"],
            "status": status,
            "error": error,
        }

    @classmethod
    def _cached_result(cls, record):
        public = cls._public_request(record)
        if public["status"] == "SUBMITTED":
            return {
                "ok": True,
                "request_id": public["request_id"],
                "task_id": public["task_id"],
                "instance_id": public["instance_id"],
                "status": "SUBMITTED",
                "cached": True,
            }
        return {
            "ok": False,
            "error": public["error"] or "submission_unknown",
            "request_id": public["request_id"],
            "task_id": public["task_id"],
            "instance_id": public["instance_id"],
            "status": public["status"],
            "cached": True,
        }

    def _lookup_request(self, epoch, request_id, fingerprint):
        try:
            with self._transaction() as state:
                record = self._find_request(state, request_id)
                if record is None:
                    return None
                if record["epoch"] != epoch:
                    return _failure("epoch_changed")
                if record["fingerprint"] != fingerprint:
                    return _failure("request_conflict")
                return self._cached_result(record)
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

    def _observer_status(self, epoch):
        if self._is_closed():
            return None, "closed"
        try:
            result = self.observer.status(epoch)
        except Exception:
            return None, "observer_failed"
        if type(result) is not dict:
            return None, "observer_failed"
        if result.get("ok") is not True:
            return None, _safe_error(result.get("error"))
        jobs = result.get("jobs")
        if type(jobs) is not list:
            return None, "observer_failed"
        required = {"task_id", "instance_id", "name", "enabled", "paused", "agent_status", "last_error"}
        for job in jobs:
            if type(job) is not dict or set(job) != required:
                return None, "observer_failed"
            if type(job["enabled"]) is not bool or type(job["name"]) is not str:
                return None, "observer_failed"
        return jobs, None

    @staticmethod
    def _check_observer_name(jobs, task_id, instance_id, name):
        matching_enabled = False
        for job in jobs:
            if not job["enabled"] or job["name"] != name:
                continue
            if (job["task_id"], job["instance_id"]) == (task_id, instance_id):
                matching_enabled = True
            else:
                return False, "agent_already_watched"
        return matching_enabled, None

    @staticmethod
    def _check_history(state, epoch, binding, jobs, task_id, instance_id):
        for record in state["requests"]:
            if record["binding"] != binding or record["state"] == "REFUSED":
                continue
            if record["epoch"] != epoch:
                if record["state"] in {"PREPARED", "SENDING", "UNKNOWN", "SUBMITTED"}:
                    return False, "agent_busy_unknown"
                continue
            if record["state"] in {"PREPARED", "SENDING", "UNKNOWN"}:
                return False, "agent_busy_unknown"
            if record["state"] == "SUBMITTED":
                retired = any(
                    job["task_id"] == record["task_id"]
                    and job["instance_id"] == record["instance_id"]
                    and job["enabled"] is False
                    for job in jobs
                )
                if not retired:
                    return False, "agent_busy"
        matching_enabled, name_error = Dispatcher._check_observer_name(
            jobs, task_id, instance_id, binding["name"]
        )
        if name_error:
            return False, name_error
        return matching_enabled, None

    def _reader_agent(self, binding):
        if self._is_closed():
            return None, "closed"
        try:
            result = self.reader(binding)
        except Exception:
            return None, "unavailable"
        if type(result) is not dict:
            return None, "invalid_response"
        if result.get("ok") is not True:
            return None, _safe_error(result.get("error"))
        agent = result.get("agent")
        if type(agent) is not dict or set(agent) != _AGENT_FIELDS:
            return None, "invalid_response"
        for field in ("name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id"):
            if not _utf8(agent[field]) or not agent[field]:
                return None, "identity_unknown"
        if any(
            agent[field] != binding[field]
            for field in ("name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id")
        ):
            return None, "identity_mismatch"
        if type(agent["agent_status"]) is not str or agent["agent_status"] not in {"idle", "working", "blocked", "done"}:
            return None, "state_unknown"
        for field in ("state_change_seq", "completion_seq"):
            value = agent[field]
            if value is not None and (type(value) is not int or value < 0):
                return None, "invalid_response"
        if agent["interactive_ready"] is not None and type(agent["interactive_ready"]) is not bool:
            return None, "invalid_response"
        if agent["agent_status"] not in {"idle", "done"} or agent["interactive_ready"] is not True:
            return None, "agent_not_ready"
        return agent, None

    def _transition(self, request_id, fingerprint, state_name, error):
        try:
            with self._transaction() as state:
                record = self._find_request(state, request_id)
                if record is None or record["fingerprint"] != fingerprint:
                    raise _StateFailure("state_invalid")
                record["state"] = state_name
                record["error"] = error
                self._write_state_locked(state)
                return state_name, error
        except _StateFailure as exc:
            return None, exc.code
        except Exception:
            return None, "state_io_error"

    def _response(self, record, state_name, error, *, cached=False, success=False):
        base = {
            "request_id": record["request_id"],
            "task_id": record["task_id"],
            "instance_id": record["instance_id"],
            "status": state_name,
            "cached": cached,
        }
        if success and state_name == "SUBMITTED":
            return {"ok": True, **base}
        return {"ok": False, "error": error, **base}

    def _finish(self, record, state_name, error, authority_check):
        authority_error = self._authority_error(record["epoch"], authority_check)
        if authority_error:
            state_name = "UNKNOWN"
            error = (
                authority_error
                if authority_error in {"state_invalid", "state_io_error"}
                else "submission_unknown"
            )
        target_state, target_error = self._transition(
            record["request_id"],
            record["fingerprint"],
            state_name,
            error,
        )
        if target_state is None:
            shown = "state_io_error" if target_error == "state_io_error" else target_error
            return self._response(record, "UNKNOWN", shown, cached=False)
        if target_state == "UNKNOWN" and target_error is None:
            target_error = "submission_unknown"
        if target_state == "SUBMITTED":
            authority_error = self._authority_error(record["epoch"], authority_check)
            if authority_error:
                target_state = "UNKNOWN"
                target_error = (
                    authority_error
                    if authority_error in {"state_invalid", "state_io_error"}
                    else "submission_unknown"
                )
                updated_state, _ = self._transition(
                    record["request_id"], record["fingerprint"], "UNKNOWN", target_error
                )
                if updated_state is None:
                    return self._response(record, "UNKNOWN", "state_io_error", cached=False)
        return self._response(
            record,
            target_state,
            target_error,
            cached=False,
            success=target_state == "SUBMITTED",
        )

    def _refuse(self, record, error, *, tracked_new=False):
        error = _safe_error(error)
        state_name, write_error = self._transition(record["request_id"], record["fingerprint"], "REFUSED", error)
        if tracked_new:
            try:
                self.observer.untrack(record["task_id"], record["instance_id"], epoch=record["epoch"])
            except Exception:
                pass
        if state_name is None:
            return self._response(record, "UNKNOWN", write_error, cached=False)
        return self._response(record, "REFUSED", error, cached=False)

    @staticmethod
    def _prompt_identity_matches(agent, binding):
        if type(agent) is not dict:
            return False
        agent_session = agent.get("agent_session")
        return (
            agent.get("name") == binding["name"]
            and agent.get("agent") == binding["agent_kind"]
            and agent.get("workspace_id") == binding["workspace_id"]
            and agent.get("tab_id") == binding["tab_id"]
            and agent.get("pane_id") == binding["pane_id"]
            and type(agent_session) is dict
            and agent_session.get("kind") == "id"
            and agent_session.get("agent") == binding["agent_kind"]
            and agent_session.get("value") == binding["agent_session_id"]
        )

    @classmethod
    def _prompt_was_accepted(cls, process, binding):
        try:
            returncode = process.returncode
            stdout = process.stdout
        except Exception:
            return False
        if type(returncode) is not int or returncode != 0 or type(stdout) is not str:
            return False
        try:
            response = json.loads(
                stdout,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
            return False
        if type(response) is not dict or "error" in response:
            return False
        result = response.get("result")
        if type(result) is not dict or result.get("type") != "agent_prompted":
            return False
        return cls._prompt_identity_matches(result.get("agent"), binding)

    def dispatch(self, *, epoch, request_id, binding, task, jev_model, owner_checks, authority_check=None):
        if self._is_closed():
            return _failure("closed")
        if type(epoch) is not str or _EPOCH_PATTERN.fullmatch(epoch) is None:
            return _failure("invalid_input")
        if not _valid_request_id(request_id) or not _valid_binding(binding):
            return _failure("invalid_input")
        if not _utf8(jev_model) or not jev_model or any(character in jev_model for character in ("\x00", "\r", "\n")):
            return _failure("invalid_input")
        if not _valid_owner_checks(owner_checks):
            return _failure("invalid_input")
        try:
            validate_task(task)
        except Exception as exc:
            return _failure(_safe_error(getattr(exc, "code", "TASK_PACKAGE_INVALID")))
        if type(task) is not dict or type(task.get("schema_version")) is not int or task.get("schema_version") != 2:
            return _failure("TASK_PACKAGE_INVALID")
        try:
            task_text = canonical_json(task)
            task_bytes = task_text.encode("utf-8")
            binding_text = canonical_json(binding)
            payload = canonical_json([epoch, binding, task, jev_model, owner_checks]).encode("utf-8")
        except (TaskContractError, UnicodeEncodeError, TypeError, ValueError):
            return _failure("TASK_PACKAGE_INVALID")
        if len(task_bytes) > _MAX_TASK_BYTES:
            return _failure("invalid_input")
        instance_id = hashlib.sha256(binding_text.encode("utf-8")).hexdigest()
        record = {
            "request_id": request_id,
            "epoch": epoch,
            "binding": dict(binding),
            "instance_id": instance_id,
            "task_id": task["task_id"],
            "revision": task["revision"],
            "task_sha256": hashlib.sha256(task_bytes).hexdigest(),
            "jev_model": jev_model,
            "owner_checks": dict(owner_checks),
            "fingerprint": hashlib.sha256(payload).hexdigest(),
            "state": "PREPARED",
            "error": None,
        }

        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return _failure(authority_error)
        cached = self._lookup_request(epoch, request_id, record["fingerprint"])
        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return _failure(authority_error)
        if cached is not None:
            return cached

        try:
            require_approval(task, jev_model, self.data_dir)
        except Exception as exc:
            return _failure(_safe_error(getattr(exc, "code", "TASK_GATE_REQUIRED")))

        jobs, observer_error = self._observer_status(epoch)
        if observer_error:
            return _failure(observer_error)
        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return _failure(authority_error)
        try:
            with self._transaction() as state:
                existing = self._find_request(state, request_id)
                if existing is not None:
                    if existing["epoch"] != epoch:
                        return _failure("epoch_changed")
                    if existing["fingerprint"] != record["fingerprint"]:
                        return _failure("request_conflict")
                    return self._cached_result(existing)
                matching_enabled, busy_error = self._check_history(
                    state, epoch, binding, jobs, record["task_id"], record["instance_id"]
                )
                if busy_error:
                    return _failure(busy_error)
                state["requests"].append(record)
                self._write_state_locked(state)
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return self._refuse(record, authority_error)
        _, read_error = self._reader_agent(binding)
        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return self._refuse(record, authority_error)
        if read_error:
            return self._refuse(record, read_error)

        track_result = None
        try:
            track_result = self.observer.track(record["task_id"], instance_id, binding, epoch=epoch)
        except Exception:
            authority_error = self._authority_error(epoch, authority_check)
            return self._refuse(record, authority_error or "observer_failed")
        if type(track_result) is not dict:
            authority_error = self._authority_error(epoch, authority_check)
            return self._refuse(record, authority_error or "observer_failed")
        if track_result.get("ok") is not True:
            authority_error = self._authority_error(epoch, authority_check)
            return self._refuse(record, authority_error or _safe_error(track_result.get("error")))
        tracked_new = not matching_enabled

        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return self._refuse(record, authority_error, tracked_new=tracked_new)
        _, read_error = self._reader_agent(binding)
        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return self._refuse(record, authority_error, tracked_new=tracked_new)
        if read_error:
            return self._refuse(record, read_error, tracked_new=tracked_new)

        latest_jobs, observer_error = self._observer_status(epoch)
        if observer_error:
            return self._refuse(record, observer_error, tracked_new=tracked_new)
        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return self._refuse(record, authority_error, tracked_new=tracked_new)
        _, busy_error = self._check_observer_name(
            latest_jobs, record["task_id"], instance_id, binding["name"]
        )
        if busy_error:
            return self._refuse(record, busy_error, tracked_new=tracked_new)

        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return self._refuse(record, authority_error, tracked_new=tracked_new)
        try:
            with self._transaction() as state:
                current = self._find_request(state, request_id)
                if current is None or current["fingerprint"] != record["fingerprint"]:
                    raise _StateFailure("state_invalid")
                current["state"] = "SENDING"
                current["error"] = None
                self._write_state_locked(state)
        except _StateFailure as exc:
            if tracked_new:
                try:
                    self.observer.untrack(record["task_id"], instance_id, epoch=epoch)
                except Exception:
                    pass
            return self._response(record, "UNKNOWN", exc.code, cached=False)
        except Exception:
            if tracked_new:
                try:
                    self.observer.untrack(record["task_id"], instance_id, epoch=epoch)
                except Exception:
                    pass
            return self._response(record, "UNKNOWN", "state_io_error", cached=False)
        authority_error = self._authority_error(epoch, authority_check)
        if authority_error:
            return self._refuse(record, authority_error, tracked_new=tracked_new)
        argv = ["herdr", "--session", binding["herdr_session"], "agent", "prompt", binding["name"], task_text]
        command_runner = subprocess.run if self.runner is None else self.runner
        try:
            process = command_runner(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=45,
            )
            accepted = self._prompt_was_accepted(process, binding)
        except Exception:
            accepted = False

        return self._finish(
            record,
            "SUBMITTED" if accepted else "UNKNOWN",
            None if accepted else "submission_unknown",
            authority_check,
        )

    def status(self, epoch=None):
        if epoch is None:
            return {
                "ok": True,
                "closed": self._is_closed(),
                "requests": [],
                "orphaned_count": 0,
            }
        if type(epoch) is not str or _EPOCH_PATTERN.fullmatch(epoch) is None:
            return _failure("invalid_input")
        try:
            with self._transaction() as state:
                current = [record for record in state["requests"] if record["epoch"] == epoch]
                orphaned_count = len(state["requests"]) - len(current)
                requests = [self._public_request(record) for record in current]
            return {
                "ok": True,
                "closed": self._is_closed(),
                "requests": requests,
                "orphaned_count": orphaned_count,
            }
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")
