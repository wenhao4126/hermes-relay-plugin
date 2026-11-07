"""为已批准任务安全创建原生空 CLI；从不向新会话发送任务正文。"""

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
from .gate_backend.task_contract import canonical_json, validate_task


_ROOT_FIELDS = {"schema_version", "requests"}
_SCHEMA_VERSION = 1
_STATE_NAME = "create_requests.json"
_LOCK_NAME = "create_requests.lock"
_MAX_BYTES = 1024 * 1024
_MAX_RECORDS = 64
_REQUEST_RE = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_EPOCH_RE = re.compile(r"epoch-[1-9][0-9]*\Z")
_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA_RE = re.compile(r"[a-f0-9]{64}\Z")
_ROLES = {"worker", "fixer", "tester", "reviewer", "auditor"}
_AUTHORITY_ERRORS = {"not_owner", "epoch_changed", "closed", "paused", "state_invalid", "state_io_error"}
_ERRORS = {
    "invalid_input", "not_owner", "epoch_changed", "closed", "paused", "state_invalid", "state_io_error",
    "request_conflict", "resource_limit", "agent_busy_unknown", "agent_exists", "unavailable",
    "invalid_response", "agent_not_ready", "identity_unknown", "identity_mismatch", "TASK_GATE_REQUIRED",
    "TASK_PACKAGE_INVALID", "DOC_REQUIRED_MISSING", "DOC_CONFLICT", "DOC_VERSION_MISMATCH",
    "DOC_DIGEST_MISMATCH", "DOC_RECORD_INVALID", "DOC_ID_CONFLICT", "GATE_RECORD_INVALID",
    "GATE_STATE_INVALID", "TASK_GATE_CONFIG_INVALID", "TASK_GATE_RESPONSE_INVALID",
    "TASK_GATE_ADJUDICATION_INVALID", "TASK_GATE_ADJUDICATION_NOT_ALLOWED",
}
_RECORD_FIELDS = {
    "request_id", "epoch", "fingerprint", "task_sha256", "task_id", "revision", "name", "role",
    "agent_kind", "herdr_session", "workspace_id", "reasoning_effort", "requested_model", "state", "error",
    "tab_id", "pane_id", "binding", "working_directory",
}


class _StateFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(error):
    return {"ok": False, "error": error}


def _utf8(value):
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_text(value, limit, *, forbid_leading_dash=False):
    return (
        _utf8(value) and 0 < len(value) <= limit and bool(value.strip())
        and not any(char in value for char in ("\x00", "\r", "\n"))
        and not (forbid_leading_dash and value.startswith("-"))
    )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 键")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(value)


def _child_environment():
    allowed = {"PATH", "HOME", "LANG"}
    result = {
        key: value for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_") or key.startswith("XDG_")
    }
    result.setdefault("PATH", os.defpath)
    return result


def _process_json(process):
    try:
        code, stdout, stderr = process.returncode, process.stdout, process.stderr
    except Exception:
        return None, "invalid_response"
    if type(code) is not int or type(stdout) is not str or type(stderr) is not str:
        return None, "invalid_response"
    streams = (stdout,) if code == 0 else (stderr, stdout)
    for text in streams:
        try:
            value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        except Exception:
            continue
        if type(value) is dict:
            return (code, value), None
    return ((code, None), None) if code != 0 else (None, "invalid_response")


def _response_error(response):
    error = response.get("error") if type(response) is dict else None
    if type(error) is dict and type(error.get("code")) is str:
        return error["code"]
    return None


def _valid_record(record):
    if type(record) is not dict or set(record) != _RECORD_FIELDS:
        return False
    if type(record["request_id"]) is not str or _REQUEST_RE.fullmatch(record["request_id"]) is None:
        return False
    if record["request_id"].startswith("-"):
        return False
    if type(record["epoch"]) is not str or _EPOCH_RE.fullmatch(record["epoch"]) is None:
        return False
    if any(type(record[key]) is not str or _SHA_RE.fullmatch(record[key]) is None
           for key in ("fingerprint", "task_sha256")):
        return False
    if type(record["task_id"]) is not str or _TASK_ID_RE.fullmatch(record["task_id"]) is None:
        return False
    if type(record["revision"]) is not int or record["revision"] < 1:
        return False
    if type(record["name"]) is not str or _NAME_RE.fullmatch(record["name"]) is None:
        return False
    if type(record["role"]) is not str or record["role"] not in _ROLES:
        return False
    if type(record["agent_kind"]) is not str or record["agent_kind"] not in {"codex", "opencode", "claude"}:
        return False
    for key in ("herdr_session", "workspace_id", "reasoning_effort", "requested_model"):
        if not _utf8(record[key]) or not record[key]:
            return False
    if not _valid_text(record["working_directory"], 4096) or not Path(record["working_directory"]).is_absolute():
        return False
    if record["reasoning_effort"] not in {"high", "max"}:
        return False
    expected_kind = "codex" if record["role"] in {"worker", "fixer"} else "opencode" if record["role"] == "tester" else "claude"
    if record["agent_kind"] != expected_kind:
        return False
    if type(record["state"]) is not str or record["state"] not in {"PREPARED", "TAB_CREATING", "STARTING", "CREATED_UNVERIFIED", "REFUSED", "UNKNOWN"}:
        return False
    if record["error"] is not None and (type(record["error"]) is not str or record["error"] not in _ERRORS):
        return False
    for key in ("tab_id", "pane_id"):
        if record[key] is not None and not _valid_text(record[key], 256):
            return False
    binding = record["binding"]
    if binding is not None:
        if type(binding) is not dict or set(binding) != {
            "herdr_session", "name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id",
        } or any(not _valid_text(value, 256) for value in binding.values()):
            return False
        expected_binding = {
            "herdr_session": record["herdr_session"], "name": record["name"],
            "agent_kind": record["agent_kind"], "workspace_id": record["workspace_id"],
            "tab_id": record["tab_id"], "pane_id": record["pane_id"],
        }
        if any(binding[key] != value for key, value in expected_binding.items()):
            return False
    if record["state"] in {"PREPARED", "TAB_CREATING", "STARTING", "CREATED_UNVERIFIED"} and record["error"] is not None:
        return False
    if record["state"] in {"UNKNOWN", "REFUSED"} and record["error"] is None:
        return False
    if (record["tab_id"] is None) != (record["pane_id"] is None):
        return False
    if record["state"] == "CREATED_UNVERIFIED" and binding is None:
        return False
    return True


def _validate_state(state):
    if type(state) is not dict or set(state) != _ROOT_FIELDS:
        raise _StateFailure("state_invalid")
    if type(state["schema_version"]) is not int or state["schema_version"] != _SCHEMA_VERSION:
        raise _StateFailure("state_invalid")
    if type(state["requests"]) is not list or len(state["requests"]) > _MAX_RECORDS:
        raise _StateFailure("state_invalid")
    seen = set()
    for record in state["requests"]:
        if not _valid_record(record) or record["request_id"] in seen:
            raise _StateFailure("state_invalid")
        seen.add(record["request_id"])


def _empty_state():
    return {"schema_version": _SCHEMA_VERSION, "requests": []}


class Creator:
    """显式profile内保存创建意图；未知副作用不重试，也不删除代理。"""

    def __init__(self, data_dir, get_runtime, *, runner=None):
        if not isinstance(data_dir, Path) or not data_dir.is_absolute() or not _utf8(str(data_dir)) or "\x00" in str(data_dir):
            raise ValueError("invalid_input")
        if not callable(get_runtime) or (runner is not None and not callable(runner)):
            raise ValueError("invalid_input")
        self.data_dir = data_dir
        self.state_path = data_dir / _STATE_NAME
        self.lock_path = data_dir / _LOCK_NAME
        self.get_runtime = get_runtime
        self.runner = runner
        self._lock = threading.RLock()
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True

    def _active_error(self, epoch):
        with self._lock:
            if self._closed:
                return "closed"
        try:
            runtime = self.get_runtime()
            active = getattr(runtime, "active_epoch", None) if runtime is not None else None
        except Exception:
            return "epoch_changed"
        if active is None:
            return "paused"
        return None if type(active) is str and active == epoch else "epoch_changed"

    def _authorized(self, epoch, authority_check):
        active = self._active_error(epoch)
        if active:
            return active
        if not callable(authority_check):
            return "not_owner"
        try:
            value = authority_check()
        except Exception:
            return "not_owner"
        if value is None:
            return self._active_error(epoch)
        if type(value) is str and value in _AUTHORITY_ERRORS:
            return value
        return "not_owner"

    def _ensure_directory(self, create):
        if self.data_dir.is_symlink():
            raise _StateFailure("state_invalid")
        try:
            info = os.lstat(self.data_dir)
        except FileNotFoundError:
            if not create:
                return False
            self.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = os.lstat(self.data_dir)
        except OSError:
            raise _StateFailure("state_io_error") from None
        if not stat.S_ISDIR(info.st_mode):
            raise _StateFailure("state_invalid")
        if stat.S_IMODE(info.st_mode) & 0o077:
            try:
                os.chmod(self.data_dir, 0o700)
            except OSError:
                raise _StateFailure("state_io_error") from None
        return True

    @contextmanager
    def _locked(self, *, create):
        with self._lock:
            exists = self._ensure_directory(create)
            if not exists:
                yield _empty_state()
                return
            try:
                metadata = os.lstat(self.lock_path)
            except FileNotFoundError:
                metadata = None
            except OSError:
                raise _StateFailure("state_io_error") from None
            if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                raise _StateFailure("state_invalid")
            if not create and metadata is None:
                yield self._read_state_locked()
                return
            try:
                state_metadata = os.lstat(self.state_path)
            except FileNotFoundError:
                state_metadata = None
            except OSError:
                raise _StateFailure("state_io_error") from None
            if state_metadata is None and not create and metadata is None:
                yield _empty_state()
                return
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            if create:
                flags |= os.O_CREAT
            fd = -1
            try:
                fd = os.open(self.lock_path, flags, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _StateFailure("state_invalid")
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "a", closefd=False) as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield self._read_state_locked()
            except _StateFailure:
                raise
            except OSError as exc:
                code = "state_invalid" if exc.errno in {errno.ELOOP, errno.ENXIO, errno.EISDIR} else "state_io_error"
                raise _StateFailure(code) from None
            finally:
                if fd >= 0:
                    os.close(fd)

    def _read_state_locked(self):
        try:
            info = os.lstat(self.state_path)
        except FileNotFoundError:
            return _empty_state()
        except OSError:
            raise _StateFailure("state_io_error") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _StateFailure("state_invalid")
        fd = -1
        try:
            fd = os.open(self.state_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _StateFailure("state_invalid")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise _StateFailure("state_invalid")
            state = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
            _validate_state(state)
            return state
        except _StateFailure:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
            raise _StateFailure("state_invalid") from None
        except OSError as exc:
            code = "state_invalid" if exc.errno in {errno.ELOOP, errno.ENXIO, errno.EISDIR} else "state_io_error"
            raise _StateFailure(code) from None
        finally:
            if fd >= 0:
                os.close(fd)

    def _write_state_locked(self, state):
        _validate_state(state)
        try:
            raw = (json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(raw) > _MAX_BYTES:
                raise _StateFailure("state_invalid")
            fd, name = tempfile.mkstemp(prefix="create_requests.", suffix=".tmp", dir=self.data_dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.state_path)
                os.chmod(self.state_path, 0o600)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            directory_fd = os.open(self.data_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except _StateFailure:
            raise
        except OSError:
            raise _StateFailure("state_io_error") from None

    def _read_only(self):
        with self._locked(create=False) as state:
            return state

    def status(self, epoch=None):
        with self._lock:
            closed = self._closed
        if epoch is None:
            return {"ok": True, "closed": closed, "requests": [], "orphaned_count": 0}
        if type(epoch) is not str or _EPOCH_RE.fullmatch(epoch) is None:
            return _fail("invalid_input")
        try:
            state = self._read_only()
            requests = [self._public(record) for record in state["requests"] if record["epoch"] == epoch]
            old = sum(1 for record in state["requests"] if record["epoch"] != epoch)
            return {"ok": True, "closed": closed, "requests": requests, "orphaned_count": old}
        except _StateFailure as exc:
            return _fail(exc.code)
        except Exception:
            return _fail("state_io_error")

    @staticmethod
    def _public(record):
        return {key: record[key] for key in (
            "request_id", "task_id", "name", "role", "state", "error", "tab_id", "pane_id",
            "requested_model", "reasoning_effort",
        )}

    def _runner(self, argv, timeout):
        runner = subprocess.run if self.runner is None else self.runner
        return runner(argv, shell=False, capture_output=True, text=True, check=False,
                      timeout=timeout, env=_child_environment())

    def _invoke(self, argv, timeout):
        try:
            process = self._runner(argv, timeout)
        except Exception:
            return None, "unavailable"
        return _process_json(process)

    @staticmethod
    def _agent(response):
        if _response_error(response):
            return None
        result = response.get("result") if type(response) is dict else None
        agent = result.get("agent") if type(result) is dict else None
        return agent if type(agent) is dict else None

    @staticmethod
    def _agent_not_found(response):
        return _response_error(response) == "agent_not_found"

    @staticmethod
    def _kind(role):
        return "codex" if role in {"worker", "fixer"} else "opencode" if role == "tester" else "claude"

    @staticmethod
    def _requested(role, effort):
        if role in {"worker", "fixer"}:
            return "gpt-6-luna", effort
        if role == "tester":
            return "cli_default_unverified", "high"
        return "cli_default_unverified", "high"

    @staticmethod
    def _agent_argv(role, effort):
        if role in {"worker", "fixer"}:
            instructions = "仅实现已派完整任务；禁止再次编排、启动子代理或改变既定架构。"
            return [
                "codex", "-m", "gpt-6-luna", "-c", f"model_reasoning_effort={effort}",
                "--disable", "multi_agent", "--disable", "multi_agent_v2", "--sandbox", "workspace-write",
                "--ask-for-approval", "on-request", "-c", "developer_instructions=" + json.dumps(instructions, ensure_ascii=False),
            ]
        if role == "tester":
            return ["opencode"]
        return [
            "claude", "--effort", "high", "--tools", "Read",
            "--allowedTools", "Read", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ]

    def _claim(self, record):
        try:
            with self._locked(create=True) as state:
                for old in state["requests"]:
                    if old["request_id"] == record["request_id"]:
                        if old["epoch"] != record["epoch"]:
                            return None, _fail("epoch_changed"), old
                        if old["fingerprint"] != record["fingerprint"]:
                            return None, _fail("request_conflict"), old
                        return None, self._cached(old), old
                for old in state["requests"]:
                    if old["name"] == record["name"]:
                        if old["state"] in {"PREPARED", "TAB_CREATING", "STARTING", "CREATED_UNVERIFIED", "UNKNOWN", "REFUSED"}:
                            return None, _fail("agent_busy_unknown"), old
                if len(state["requests"]) >= _MAX_RECORDS:
                    return None, _fail("resource_limit"), None
                state["requests"].append(record)
                self._write_state_locked(state)
                return record, None, None
        except _StateFailure as exc:
            return None, _fail(exc.code), None
        except Exception:
            return None, _fail("state_io_error"), None

    @staticmethod
    def _cached(record):
        state = record["state"]
        if state in {"PREPARED", "TAB_CREATING", "STARTING", "UNKNOWN"}:
            return {
                "ok": False, "status": "UNKNOWN", "error": record["error"] or "agent_busy_unknown",
                "request_id": record["request_id"], "task_id": record["task_id"],
                "tab_id": record["tab_id"], "pane_id": record["pane_id"], "cached": True,
            }
        if state == "CREATED_UNVERIFIED":
            value = {
                "ok": True, "status": state, "request_id": record["request_id"],
                "task_id": record["task_id"], "tab_id": record["tab_id"], "pane_id": record["pane_id"],
                "cached": True, "needs_owner_verification": True, "binding": record["binding"],
                "requested_model": record["requested_model"], "reasoning_effort": record["reasoning_effort"],
                "requested_effort": record["reasoning_effort"],
            }
            return value
        return {
            "ok": False, "status": state, "error": record["error"] or "agent_busy_unknown",
            "request_id": record["request_id"], "task_id": record["task_id"],
            "tab_id": record["tab_id"], "pane_id": record["pane_id"], "cached": True,
        }

    def _transition(self, request_id, epoch, fingerprint, **updates):
        try:
            with self._locked(create=True) as state:
                record = next((item for item in state["requests"] if item["request_id"] == request_id), None)
                if record is None or record["epoch"] != epoch or record["fingerprint"] != fingerprint:
                    raise _StateFailure("state_invalid")
                record.update(updates)
                self._write_state_locked(state)
                return dict(record), None
        except _StateFailure as exc:
            return None, exc.code
        except Exception:
            return None, "state_io_error"

    def _response(self, record, *, ok, status, error=None, cached=False):
        value = {"ok": ok, "status": status, "request_id": record["request_id"],
                 "task_id": record["task_id"], "tab_id": record["tab_id"], "pane_id": record["pane_id"],
                 "cached": cached}
        if error is not None:
            value["error"] = error
        if ok and record["binding"] is not None:
            value.update(needs_owner_verification=True, binding=record["binding"],
                         requested_model=record["requested_model"], reasoning_effort=record["reasoning_effort"])
            value["requested_effort"] = record["reasoning_effort"]
        return value

    def _finish_unknown(self, record, error):
        updated, write_error = self._transition(
            record["request_id"], record["epoch"], record["fingerprint"], state="UNKNOWN", error=error,
        )
        if updated is None:
            return self._response(record, ok=False, status="UNKNOWN", error=write_error or "state_io_error")
        return self._response(updated, ok=False, status="UNKNOWN", error=error)

    @staticmethod
    def _valid_create_args(epoch, request_id, herdr_session, workspace_id, name, role, reasoning_effort, task, jev_model):
        if type(epoch) is not str or _EPOCH_RE.fullmatch(epoch) is None:
            return False
        if type(request_id) is not str or _REQUEST_RE.fullmatch(request_id) is None or request_id.startswith("-"):
            return False
        if not _valid_text(herdr_session, 256, forbid_leading_dash=True):
            return False
        if not _valid_text(workspace_id, 256, forbid_leading_dash=True):
            return False
        if type(name) is not str or _NAME_RE.fullmatch(name) is None or type(role) is not str or role not in _ROLES:
            return False
        if type(reasoning_effort) is not str or reasoning_effort not in {"high", "max"}:
            return False
        if role in {"tester", "reviewer", "auditor"} and reasoning_effort != "high":
            return False
        return type(task) is dict and task.get("schema_version") == 2 and _valid_text(jev_model, 128)

    def _record_base(self, *, epoch, request_id, task, model, herdr_session, workspace_id, name, role, effort, fingerprint, task_sha):
        kind = self._kind(role)
        requested_model, requested_effort = self._requested(role, effort)
        return {
            "request_id": request_id, "epoch": epoch, "fingerprint": fingerprint,
            "task_sha256": task_sha, "task_id": task["task_id"], "revision": task["revision"],
            "name": name, "role": role, "agent_kind": kind, "herdr_session": herdr_session,
            "workspace_id": workspace_id, "reasoning_effort": requested_effort,
            "requested_model": requested_model, "state": "PREPARED", "error": None,
            "tab_id": None, "pane_id": None, "binding": None,
            "working_directory": task["working_directory"],
        }

    def _preflight(self, session, name):
        parsed, error = self._invoke(["herdr", "--session", session, "agent", "get", name], 10)
        if error:
            return None, error
        code, response = parsed
        if response is not None and self._agent_not_found(response):
            return True, None
        if code == 0:
            agent = self._agent(response) if response else None
            if agent is not None:
                return None, "agent_exists"
            return None, "invalid_response"
        return None, "unavailable"

    @staticmethod
    def _tab_ids(response, workspace_id):
        result = response.get("result") if type(response) is dict else None
        tab = result.get("tab") if type(result) is dict else None
        root_pane = result.get("root_pane") if type(result) is dict else None
        tab_id = tab.get("tab_id") if type(tab) is dict else None
        pane_id = root_pane.get("pane_id") if type(root_pane) is dict else None
        if not _valid_text(tab_id, 256) or not _valid_text(pane_id, 256):
            return None
        found = []
        def visit(value):
            if type(value) is dict:
                for key, item in value.items():
                    if key == "workspace_id" and type(item) is str:
                        found.append(item)
                    visit(item)
            elif type(value) is list:
                for item in value:
                    visit(item)
        visit(response)
        if any(value != workspace_id for value in found):
            return None
        return tab_id, pane_id

    def _fresh_binding(self, response, record):
        agent = self._agent(response)
        if agent is None:
            return None, "invalid_response"
        expected = {"name": record["name"], "agent": record["agent_kind"],
                    "workspace_id": record["workspace_id"], "tab_id": record["tab_id"],
                    "pane_id": record["pane_id"]}
        for key, value in expected.items():
            observed = agent.get(key)
            if not _valid_text(observed, 256):
                return None, "identity_unknown"
            if observed != value:
                return None, "identity_mismatch"
        session = agent.get("agent_session")
        if type(session) is not dict:
            return None, "identity_unknown"
        kind, agent_kind, session_id = session.get("kind"), session.get("agent"), session.get("value")
        if kind != "id" or not _valid_text(agent_kind, 32) or not _valid_text(session_id, 256):
            return None, "agent_not_ready"
        if agent_kind != record["agent_kind"]:
            return None, "identity_mismatch"
        if agent.get("interactive_ready") is not True:
            return None, "agent_not_ready"
        status = agent.get("agent_status")
        if type(status) is not str or status not in {"idle", "done"}:
            return None, "agent_not_ready"
        cwd = record.get("working_directory")
        if "cwd" in agent and agent["cwd"] != cwd:
            return None, "identity_mismatch"
        if "foreground_cwd" in agent and agent["foreground_cwd"] != cwd:
            return None, "identity_mismatch"
        return {
            "herdr_session": record["herdr_session"], "name": record["name"],
            "agent_kind": record["agent_kind"], "workspace_id": record["workspace_id"],
            "tab_id": record["tab_id"], "pane_id": record["pane_id"],
            "agent_session_id": session_id,
        }, None

    def create(self, *, epoch, request_id, herdr_session, workspace_id, name, role,
               reasoning_effort, task, jev_model, authority_check=None):
        if not self._valid_create_args(epoch, request_id, herdr_session, workspace_id, name,
                                       role, reasoning_effort, task, jev_model):
            return _fail("invalid_input")
        error = self._authorized(epoch, authority_check)
        if error:
            return _fail(error)
        try:
            task = validate_task(task)
            if task["schema_version"] != 2:
                return _fail("TASK_PACKAGE_INVALID")
            cwd_path = Path(task["working_directory"])
            if cwd_path.is_symlink() or not cwd_path.is_absolute() or not cwd_path.is_dir() or str(cwd_path.resolve(strict=True)) != str(cwd_path):
                return _fail("TASK_PACKAGE_INVALID")
            approved = require_approval(task, jev_model, self.data_dir)
            if not _utf8(approved):
                return _fail("TASK_GATE_REQUIRED")
            canonical = canonical_json(task)
            task_bytes = canonical.encode("utf-8")
            if len(task_bytes) > 1024 * 1024:
                return _fail("invalid_input")
        except Exception as exc:
            code = getattr(exc, "code", "TASK_GATE_REQUIRED")
            return _fail(code if type(code) is str and code in _ERRORS else "TASK_GATE_REQUIRED")
        error = self._authorized(epoch, authority_check)
        if error:
            return _fail(error)
        task_sha = hashlib.sha256(task_bytes).hexdigest()
        fingerprint = hashlib.sha256(canonical_json([
            epoch, request_id, herdr_session, workspace_id, name, role, reasoning_effort, task, jev_model,
        ]).encode("utf-8")).hexdigest()
        try:
            previous_state = self._read_only()
        except _StateFailure as exc:
            return _fail(exc.code)
        except Exception:
            return _fail("state_io_error")
        error = self._authorized(epoch, authority_check)
        if error:
            return _fail(error)
        for old in previous_state["requests"]:
            if old["request_id"] == request_id:
                if old["epoch"] != epoch:
                    return _fail("epoch_changed")
                if old["fingerprint"] != fingerprint:
                    return _fail("request_conflict")
                error = self._authorized(epoch, authority_check)
                if error:
                    return _fail(error)
                return self._cached(old)
        for old in previous_state["requests"]:
            if old["name"] == name:
                return _fail("agent_busy_unknown")
        error = self._authorized(epoch, authority_check)
        if error:
            return _fail(error)
        not_found, preflight_error = self._preflight(herdr_session, name)
        error = self._authorized(epoch, authority_check)
        if error:
            return _fail(error)
        if preflight_error:
            return _fail(preflight_error)
        record = self._record_base(
            epoch=epoch, request_id=request_id, task=task, model=jev_model, herdr_session=herdr_session,
            workspace_id=workspace_id, name=name, role=role, effort=reasoning_effort,
            fingerprint=fingerprint, task_sha=task_sha,
        )
        claimed, claim_error, previous = self._claim(record)
        if claim_error:
            return claim_error
        record = claimed
        if not_found is not True:
            return _fail("agent_exists")
        error = self._authorized(epoch, authority_check)
        if error:
            return self._refuse_before_prompt(record, error)
        changed, write_error = self._transition(
            request_id, epoch, fingerprint, state="TAB_CREATING", error=None,
        )
        if changed is None:
            return self._response(record, ok=False, status="UNKNOWN", error=write_error or "state_io_error")
        record = changed
        error = self._authorized(epoch, authority_check)
        if error:
            return self._refuse_before_prompt(record, error)
        tab_argv = ["herdr", "--session", herdr_session, "tab", "create", "--workspace", workspace_id,
                    "--cwd", str(cwd_path), "--label", name]
        if role in {"reviewer", "auditor"}:
            tab_argv.extend(["--env", "CLAUDE_CODE_EFFORT_LEVEL=high"])
        tab_argv.append("--no-focus")
        parsed, call_error = self._invoke(tab_argv, 20)
        error = self._authorized(epoch, authority_check)
        if error:
            return self._finish_unknown(record, error)
        if call_error or parsed is None or parsed[0] != 0 or _response_error(parsed[1]) is not None:
            return self._finish_unknown(record, "unavailable" if call_error or parsed is None else "invalid_response")
        ids = self._tab_ids(parsed[1], workspace_id)
        if ids is None:
            return self._finish_unknown(record, "invalid_response")
        tab_id, pane_id = ids
        changed, write_error = self._transition(
            request_id, epoch, fingerprint, state="STARTING", error=None, tab_id=tab_id, pane_id=pane_id,
        )
        if changed is None:
            return self._response(record, ok=False, status="UNKNOWN", error=write_error or "state_io_error")
        record = changed
        error = self._authorized(epoch, authority_check)
        if error:
            return self._finish_unknown(record, error)
        start_argv = ["herdr", "--session", herdr_session, "agent", "start", name,
                      "--kind", record["agent_kind"], "--pane", pane_id, "--timeout", "30000", "--"]
        start_argv.extend(self._agent_argv(role, reasoning_effort)[1:])
        parsed, call_error = self._invoke(start_argv, 40)
        error = self._authorized(epoch, authority_check)
        if error:
            return self._finish_unknown(record, error)
        if call_error or parsed is None or parsed[0] != 0 or _response_error(parsed[1]) is not None:
            return self._finish_unknown(record, "unavailable" if call_error or parsed is None else "invalid_response")
        error = self._authorized(epoch, authority_check)
        if error:
            return self._finish_unknown(record, error)
        parsed, get_error = self._invoke(["herdr", "--session", herdr_session, "agent", "get", name], 10)
        error = self._authorized(epoch, authority_check)
        if error:
            return self._finish_unknown(record, error)
        if get_error or parsed is None or parsed[0] != 0:
            return self._finish_unknown(record, get_error or "unavailable")
        binding, identity_error = self._fresh_binding(parsed[1], {**record, "working_directory": str(cwd_path)})
        if identity_error:
            return self._finish_unknown(record, identity_error)
        error = self._authorized(epoch, authority_check)
        if error:
            return self._finish_unknown(record, error)
        changed, write_error = self._transition(
            request_id, epoch, fingerprint, state="CREATED_UNVERIFIED", error=None, binding=binding,
        )
        if changed is None:
            return self._response(record, ok=False, status="UNKNOWN", error=write_error or "state_io_error")
        error = self._authorized(epoch, authority_check)
        if error:
            changed, write_error = self._transition(
                request_id, epoch, fingerprint, state="UNKNOWN", error=error,
            )
            return self._response(
                changed or record, ok=False, status="UNKNOWN", error=write_error or error,
            )
        return self._response(changed, ok=True, status="CREATED_UNVERIFIED")

    def _refuse_before_prompt(self, record, error):
        changed, write_error = self._transition(
            record["request_id"], record["epoch"], record["fingerprint"], state="REFUSED", error=error,
        )
        if changed is None:
            return self._response(record, ok=False, status="UNKNOWN", error=write_error or "state_io_error")
        return self._response(changed, ok=False, status="REFUSED", error=error)

    def refresh(self, *, epoch, request_id, authority_check=None):
        if type(epoch) is not str or _EPOCH_RE.fullmatch(epoch) is None or type(request_id) is not str or _REQUEST_RE.fullmatch(request_id) is None:
            return _fail("invalid_input")
        error = self._authorized(epoch, authority_check)
        if error:
            return _fail(error)
        try:
            state = self._read_only()
        except _StateFailure as exc:
            return _fail(exc.code)
        record = next((item for item in state["requests"] if item["request_id"] == request_id), None)
        if record is None:
            return _fail("invalid_input")
        if record["epoch"] != epoch:
            return _fail("epoch_changed")
        starting_with_known_tab = (
            record["state"] == "STARTING"
            and record["binding"] is None
            and record["tab_id"] is not None
            and record["pane_id"] is not None
        )
        if (
            (record["state"] not in {"CREATED_UNVERIFIED", "UNKNOWN"} and not starting_with_known_tab)
            or record["tab_id"] is None
            or record["pane_id"] is None
        ):
            return self._cached(record)
        error = self._authorized(epoch, authority_check)
        if error:
            return self._response(record, ok=False, status="UNKNOWN", error=error)
        parsed, error = self._invoke(
            ["herdr", "--session", record["herdr_session"], "agent", "get", record["name"]], 10,
        )
        authority_error = self._authorized(epoch, authority_check)
        if authority_error:
            return self._response(record, ok=False, status="UNKNOWN", error=authority_error)
        if error or parsed is None or parsed[0] != 0:
            return self._response(record, ok=False, status="UNKNOWN", error=error or "unavailable")
        expected = {
            "request_id": request_id, "task_id": record["task_id"], "name": record["name"],
            "agent_kind": record["agent_kind"], "workspace_id": record["workspace_id"],
            "tab_id": record["tab_id"], "pane_id": record["pane_id"], "herdr_session": record["herdr_session"],
            "working_directory": record["working_directory"],
        }
        binding, identity_error = self._fresh_binding(parsed[1], expected)
        if identity_error:
            return self._response(record, ok=False, status="UNKNOWN", error=identity_error)
        if record["binding"] is not None and record["binding"]["agent_session_id"] != binding["agent_session_id"]:
            return self._response(record, ok=False, status="UNKNOWN", error="identity_mismatch")
        error = self._authorized(epoch, authority_check)
        if error:
            return self._response(record, ok=False, status="UNKNOWN", error=error)
        changed, write_error = self._transition(
            request_id, epoch, record["fingerprint"], state="CREATED_UNVERIFIED", error=None, binding=binding,
        )
        if changed is None:
            return self._response(record, ok=False, status="UNKNOWN", error=write_error or "state_io_error")
        authority_error = self._authorized(epoch, authority_check)
        if authority_error:
            changed, write_error = self._transition(
                request_id, epoch, record["fingerprint"], state="UNKNOWN", error=authority_error,
            )
            return self._response(
                changed or record, ok=False, status="UNKNOWN", error=write_error or authority_error,
            )
        return self._response(changed, ok=True, status="CREATED_UNVERIFIED", cached=True)

