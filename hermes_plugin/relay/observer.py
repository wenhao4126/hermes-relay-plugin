"""为已登记的 Herdr 代理任务提供持久化、只读观察与通知意图重放。"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .herdr_reader import read_bound_agent


_SCHEMA_VERSION = 1
_STATE_NAME = "relay-observer.json"
_LOCK_NAME = "relay-observer.lock"
_MAX_STATE_BYTES = 8 * 1024 * 1024
_MAX_ENABLED_JOBS = 64
_BINDING_FIELDS = frozenset({
    "herdr_session", "name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id",
})
_AGENT_KINDS = frozenset({"codex", "opencode", "claude"})
_AGENT_STATES = frozenset({"idle", "working", "blocked", "done"})
_NOTICE_KINDS = frozenset({"idle", "blocked", "removed", "unknown"})
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_EPOCH_PATTERN = re.compile(r"epoch-[1-9][0-9]*\Z")
_ERRORS = frozenset({
    "closed", "paused", "epoch_changed", "binding_conflict", "task_retired", "capacity_reached",
    "sequence_unknown", "sequence_regressed", "sequence_inconsistent", "state_invalid", "state_io_error",
    "invalid_input", "identity_mismatch", "identity_unknown", "not_found", "unavailable",
    "invalid_response", "state_unknown", "inject_rejected", "delivery_unknown", "event_conflict",
    "not_bound", "shutdown_pending",
})
_ROOT_FIELDS = {"schema_version", "jobs"}
_JOB_FIELDS = {
    "epoch", "task_id", "instance_id", "binding", "enabled", "paused", "last_state_change_seq",
    "last_completion_seq", "last_agent_status", "last_error", "last_blocked_seq", "notice_index",
    "pending_notice",
}
_CURSOR_FIELDS = {
    "last_state_change_seq", "last_completion_seq", "last_agent_status", "last_error",
    "last_blocked_seq", "notice_index", "paused",
}
_NOTICE_FIELDS = {"event_id", "kind", "summary", "after"}


def _failure(error):
    return {"ok": False, "error": error}


def _valid_utf8(value):
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_identifier(value):
    return _valid_utf8(value) and bool(value) and len(value) <= 128 and "\x00" not in value


def _valid_binding(binding):
    if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
        return False
    for value in binding.values():
        if not _valid_utf8(value) or not value.strip() or any(char in value for char in ("\x00", "\r", "\n")):
            return False
    if binding["agent_kind"] not in _AGENT_KINDS:
        return False
    if _NAME_PATTERN.fullmatch(binding["name"]) is None or binding["herdr_session"].startswith("-"):
        return False
    return True


def _valid_sequence(value, *, optional=False):
    return value is None and optional or type(value) is int and value >= 0


def _event_id(epoch, task_id, instance_id, next_notice_index):
    payload = json.dumps(
        [epoch, task_id, instance_id, next_notice_index],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _empty_state():
    return {"schema_version": _SCHEMA_VERSION, "jobs": []}


class _StateFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(value)


def _validate_cursor(cursor):
    if type(cursor) is not dict or set(cursor) != _CURSOR_FIELDS:
        raise _StateFailure("state_invalid")
    if not _valid_sequence(cursor["last_state_change_seq"]):
        raise _StateFailure("state_invalid")
    if not _valid_sequence(cursor["last_completion_seq"]):
        raise _StateFailure("state_invalid")
    if not _valid_sequence(cursor["last_blocked_seq"], optional=True):
        raise _StateFailure("state_invalid")
    if cursor["last_agent_status"] is not None and (
        type(cursor["last_agent_status"]) is not str or cursor["last_agent_status"] not in _AGENT_STATES
    ):
        raise _StateFailure("state_invalid")
    if cursor["last_error"] is not None and (
        type(cursor["last_error"]) is not str or cursor["last_error"] not in _ERRORS
    ):
        raise _StateFailure("state_invalid")
    if type(cursor["notice_index"]) is not int or cursor["notice_index"] < 0:
        raise _StateFailure("state_invalid")
    if type(cursor["paused"]) is not bool:
        raise _StateFailure("state_invalid")


def _validate_state(state):
    if type(state) is not dict or set(state) != _ROOT_FIELDS:
        raise _StateFailure("state_invalid")
    if type(state["schema_version"]) is not int or state["schema_version"] != _SCHEMA_VERSION:
        raise _StateFailure("state_invalid")
    if type(state["jobs"]) is not list:
        raise _StateFailure("state_invalid")

    seen = set()
    for job in state["jobs"]:
        if type(job) is not dict or set(job) != _JOB_FIELDS:
            raise _StateFailure("state_invalid")
        if type(job["epoch"]) is not str or _EPOCH_PATTERN.fullmatch(job["epoch"]) is None:
            raise _StateFailure("state_invalid")
        if not _valid_identifier(job["task_id"]) or not _valid_identifier(job["instance_id"]):
            raise _StateFailure("state_invalid")
        if not _valid_binding(job["binding"]):
            raise _StateFailure("state_invalid")
        if type(job["enabled"]) is not bool or type(job["paused"]) is not bool:
            raise _StateFailure("state_invalid")
        for field in ("last_state_change_seq", "last_completion_seq"):
            if not _valid_sequence(job[field]):
                raise _StateFailure("state_invalid")
        if job["last_agent_status"] is not None and (
            type(job["last_agent_status"]) is not str or job["last_agent_status"] not in _AGENT_STATES
        ):
            raise _StateFailure("state_invalid")
        if job["last_error"] is not None and (
            type(job["last_error"]) is not str or job["last_error"] not in _ERRORS
        ):
            raise _StateFailure("state_invalid")
        if not _valid_sequence(job["last_blocked_seq"], optional=True):
            raise _StateFailure("state_invalid")
        if type(job["notice_index"]) is not int or job["notice_index"] < 0:
            raise _StateFailure("state_invalid")

        key = (job["epoch"], job["task_id"], job["instance_id"])
        if key in seen:
            raise _StateFailure("state_invalid")
        seen.add(key)

        notice = job["pending_notice"]
        if notice is not None:
            if type(notice) is not dict or set(notice) != _NOTICE_FIELDS:
                raise _StateFailure("state_invalid")
            if type(notice["event_id"]) is not str or re.fullmatch(r"[0-9a-f]{64}", notice["event_id"]) is None:
                raise _StateFailure("state_invalid")
            if type(notice["kind"]) is not str or notice["kind"] not in _NOTICE_KINDS:
                raise _StateFailure("state_invalid")
            summary = notice["summary"]
            if not _valid_utf8(summary) or not summary or len(summary) > 512 or "\x00" in summary:
                raise _StateFailure("state_invalid")
            after = notice["after"]
            _validate_cursor(after)
            expected_index = job["notice_index"] + 1
            if after["notice_index"] != expected_index:
                raise _StateFailure("state_invalid")
            if notice["event_id"] != _event_id(job["epoch"], job["task_id"], job["instance_id"], expected_index):
                raise _StateFailure("state_invalid")


class _ObserverLock:
    def __init__(self, observer):
        self.observer = observer
        self.descriptor = -1
        self.locked = False

    def __enter__(self):
        observer = self.observer
        observer._state_lock.acquire()
        try:
            try:
                observer.data_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise _StateFailure("state_io_error") from None
            observer._existing_path_is_regular(observer.lock_path)
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                self.descriptor = os.open(observer.lock_path, flags, 0o600)
            except OSError as error:
                raise observer._open_error(error) from None
            try:
                if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
                    raise _StateFailure("state_invalid")
                os.fchmod(self.descriptor, 0o600)
                fcntl.flock(self.descriptor, fcntl.LOCK_EX)
                self.locked = True
            except _StateFailure:
                raise
            except OSError:
                raise _StateFailure("state_io_error") from None
            return self
        except Exception:
            self._release()
            raise

    def _release(self):
        if self.locked:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            self.locked = False
        if self.descriptor >= 0:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = -1
        self.observer._state_lock.release()

    def __exit__(self, exc_type, exc_value, traceback):
        self._release()
        return False


class Observer:
    """持久化既有代理任务观察游标和通知意图，不创建或控制代理。"""

    def __init__(self, data_dir, get_runtime, *, reader=read_bound_agent, can_deliver, interval=2.0):
        if not isinstance(data_dir, Path):
            raise ValueError("invalid_input")
        path_text = str(data_dir)
        try:
            path_text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid_input") from None
        if not data_dir.is_absolute() or "\x00" in path_text:
            raise ValueError("invalid_input")
        if not callable(get_runtime) or not callable(reader) or not callable(can_deliver):
            raise ValueError("invalid_input")
        try:
            interval_valid = type(interval) in (int, float) and math.isfinite(interval) and interval > 0
        except (OverflowError, ValueError):
            interval_valid = False
        if not interval_valid:
            raise ValueError("invalid_input")
        self.data_dir = data_dir
        self.state_path = data_dir / _STATE_NAME
        self.lock_path = data_dir / _LOCK_NAME
        self.get_runtime = get_runtime
        self.reader = reader
        self.can_deliver = can_deliver
        self.interval = float(interval)
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._last_error = None
        self._cursor = 0

    @staticmethod
    def _open_error(error):
        if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
            return _StateFailure("state_invalid")
        return _StateFailure("state_io_error")

    @staticmethod
    def _existing_path_is_regular(path):
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise _StateFailure("state_io_error") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise _StateFailure("state_invalid")
        return True

    def _locked(self):
        return _ObserverLock(self)

    def _read_state_locked(self):
        if not self._existing_path_is_regular(self.state_path):
            return _empty_state()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(self.state_path, flags)
        except FileNotFoundError:
            return _empty_state()
        except OSError as error:
            raise self._open_error(error) from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _StateFailure("state_invalid")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "rb", closefd=True) as state_file:
                descriptor = -1
                raw = state_file.read(_MAX_STATE_BYTES + 1)
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
        except OSError:
            raise _StateFailure("state_io_error") from None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _write_state_locked(self, state):
        _validate_state(state)
        try:
            encoded = json.dumps(
                state,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise _StateFailure("state_invalid") from None
        if len(encoded) > _MAX_STATE_BYTES:
            raise _StateFailure("state_invalid")

        descriptor = -1
        temporary_path = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(prefix=".relay-observer-", dir=str(self.data_dir))
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as temporary_file:
                descriptor = -1
                temporary_file.write(encoded)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
            directory_fd = os.open(self.data_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except _StateFailure:
            raise
        except OSError:
            raise _StateFailure("state_io_error") from None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    @contextmanager
    def _transaction(self):
        with self._locked():
            yield self._read_state_locked()

    def _runtime_for_epoch(self, epoch):
        if self._stop_event.is_set():
            return None, "closed"
        try:
            runtime = self.get_runtime()
            active_epoch = getattr(runtime, "active_epoch", None) if runtime is not None else None
        except Exception:
            return None, "state_io_error"
        if runtime is None or active_epoch is None:
            return None, "paused"
        if active_epoch != epoch:
            return None, "epoch_changed"
        return runtime, None

    def _active_runtime(self):
        if self._stop_event.is_set():
            return None, None, "closed"
        try:
            runtime = self.get_runtime()
            epoch = getattr(runtime, "active_epoch", None) if runtime is not None else None
        except Exception:
            return None, None, "state_io_error"
        if runtime is None:
            return None, None, "paused"
        if epoch is None:
            return None, None, "paused"
        if type(epoch) is not str or _EPOCH_PATTERN.fullmatch(epoch) is None:
            return None, None, "epoch_changed"
        return runtime, epoch, None

    @staticmethod
    def _job_key(job):
        return job["epoch"], job["task_id"], job["instance_id"]

    @staticmethod
    def _find_job(state, epoch, task_id, instance_id):
        for job in state["jobs"]:
            if (job["epoch"], job["task_id"], job["instance_id"]) == (epoch, task_id, instance_id):
                return job
        return None

    @staticmethod
    def _public_job(job):
        return {
            "task_id": job["task_id"],
            "instance_id": job["instance_id"],
            "name": job["binding"]["name"],
            "enabled": job["enabled"],
            "paused": job["paused"],
            "agent_status": job["last_agent_status"],
            "last_error": job["last_error"],
        }

    def _read_result(self, binding):
        try:
            result = self.reader(binding)
        except Exception:
            return None, "unavailable"
        if type(result) is not dict:
            return None, "invalid_response"
        if result.get("ok") is not True:
            error = result.get("error")
            if type(error) is str and error in _ERRORS:
                return None, error
            return None, "invalid_response"
        agent = result.get("agent")
        if type(agent) is not dict:
            return None, "invalid_response"
        identity_fields = {
            "name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id",
        }
        if not identity_fields.issubset(agent):
            return None, "identity_unknown"
        for field in identity_fields:
            if not _valid_utf8(agent[field]) or not agent[field]:
                return None, "identity_unknown"
        required = {
            *identity_fields, "agent_status", "interactive_ready", "state_change_seq", "completion_seq",
        }
        if set(agent) != required:
            return None, "invalid_response"
        if type(agent["agent_status"]) is not str or agent["agent_status"] not in _AGENT_STATES:
            return None, "state_unknown"
        if not _valid_sequence(agent["state_change_seq"], optional=True):
            return None, "invalid_response"
        if not _valid_sequence(agent["completion_seq"], optional=True):
            return None, "invalid_response"
        if agent["interactive_ready"] is not None and type(agent["interactive_ready"]) is not bool:
            return None, "invalid_response"
        if any(
            agent[field] != binding[binding_field]
            for field, binding_field in (
                ("name", "name"),
                ("agent_kind", "agent_kind"),
                ("workspace_id", "workspace_id"),
                ("tab_id", "tab_id"),
                ("pane_id", "pane_id"),
                ("agent_session_id", "agent_session_id"),
            )
        ):
            return None, "identity_mismatch"
        return agent, None

    @staticmethod
    def _cursor_from_job(job):
        return {field: job[field] for field in _CURSOR_FIELDS}

    @staticmethod
    def _summary(name, state, error, state_seq, completion_seq):
        value = {
            "agent": name,
            "state": state,
            "error": error,
            "state_change_seq": state_seq,
            "completion_seq": completion_seq,
        }
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _notice(self, job, kind, error, state, state_seq, completion_seq, after):
        next_index = job["notice_index"] + 1
        after = dict(after)
        after["notice_index"] = next_index
        summary = self._summary(job["binding"]["name"], state, error, state_seq, completion_seq)
        return {
            "event_id": _event_id(job["epoch"], job["task_id"], job["instance_id"], next_index),
            "kind": kind,
            "summary": summary,
            "after": after,
        }

    def track(self, task_id, instance_id, binding, *, epoch):
        if not _valid_identifier(task_id) or not _valid_identifier(instance_id) or not _valid_binding(binding):
            return _failure("invalid_input")
        if type(epoch) is not str or _EPOCH_PATTERN.fullmatch(epoch) is None:
            return _failure("invalid_input")
        if self._stop_event.is_set():
            return _failure("closed")
        runtime, runtime_error = self._runtime_for_epoch(epoch)
        if runtime_error:
            return _failure(runtime_error)
        try:
            with self._locked():
                state = self._read_state_locked()
                current_runtime, runtime_error = self._runtime_for_epoch(epoch)
                if runtime_error:
                    return _failure(runtime_error)
                if current_runtime is not runtime:
                    return _failure("epoch_changed")
                existing = self._find_job(state, epoch, task_id, instance_id)
                if existing is not None:
                    if not existing["enabled"]:
                        return _failure("task_retired")
                    if existing["binding"] != binding:
                        return _failure("binding_conflict")
                    return {
                        "ok": True,
                        "task_id": task_id,
                        "instance_id": instance_id,
                        "tracking": True,
                        "epoch": epoch,
                    }
                if sum(1 for job in state["jobs"] if job["enabled"]) >= _MAX_ENABLED_JOBS:
                    return _failure("capacity_reached")
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")

        if self._stop_event.is_set():
            return _failure("closed")
        result, read_error = self._read_result(binding)
        if read_error:
            return _failure(read_error)
        state_seq = result["state_change_seq"]
        if state_seq is None:
            return _failure("sequence_unknown")
        completion_seq = result["completion_seq"]
        runtime, runtime_error = self._runtime_for_epoch(epoch)
        if runtime_error:
            return _failure(runtime_error)
        if self._stop_event.is_set():
            return _failure("closed")

        job = {
            "epoch": epoch,
            "task_id": task_id,
            "instance_id": instance_id,
            "binding": dict(binding),
            "enabled": True,
            "paused": False,
            "last_state_change_seq": state_seq,
            "last_completion_seq": completion_seq if completion_seq is not None else 0,
            "last_agent_status": result["agent_status"],
            "last_error": None,
            "last_blocked_seq": None,
            "notice_index": 0,
            "pending_notice": None,
        }
        try:
            with self._locked():
                state = self._read_state_locked()
                current_runtime, runtime_error = self._runtime_for_epoch(epoch)
                if runtime_error:
                    return _failure(runtime_error)
                if self._stop_event.is_set():
                    return _failure("closed")
                existing = self._find_job(state, epoch, task_id, instance_id)
                if existing is not None:
                    if not existing["enabled"]:
                        return _failure("task_retired")
                    if existing["binding"] != binding:
                        return _failure("binding_conflict")
                    return {
                        "ok": True, "task_id": task_id, "instance_id": instance_id,
                        "tracking": True, "epoch": epoch,
                    }
                if sum(1 for item in state["jobs"] if item["enabled"]) >= _MAX_ENABLED_JOBS:
                    return _failure("capacity_reached")
                state["jobs"].append(job)
                self._write_state_locked(state)
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")
        return {"ok": True, "task_id": task_id, "instance_id": instance_id, "tracking": True, "epoch": epoch}

    def untrack(self, task_id, instance_id, *, epoch):
        if not _valid_identifier(task_id) or not _valid_identifier(instance_id):
            return _failure("invalid_input")
        if type(epoch) is not str or _EPOCH_PATTERN.fullmatch(epoch) is None:
            return _failure("invalid_input")
        if self._stop_event.is_set():
            return _failure("closed")
        runtime, runtime_error = self._runtime_for_epoch(epoch)
        if runtime_error:
            return _failure(runtime_error)
        try:
            with self._locked():
                state = self._read_state_locked()
                current_runtime, runtime_error = self._runtime_for_epoch(epoch)
                if runtime_error:
                    return _failure(runtime_error)
                job = self._find_job(state, epoch, task_id, instance_id)
                if job is None or not job["enabled"]:
                    return {"ok": True, "removed": False}
                if self._stop_event.is_set():
                    return _failure("closed")
                job["enabled"] = False
                self._write_state_locked(state)
                return {"ok": True, "removed": True}
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")

    def _pending_snapshots(self, epoch):
        with self._transaction() as state:
            return [
                copy.deepcopy(job)
                for job in state["jobs"]
                if job["epoch"] == epoch and job["enabled"] and job["pending_notice"] is not None
            ]

    def _commit_pending(self, snapshot, runtime):
        notice = snapshot["pending_notice"]
        epoch, task_id, instance_id = self._job_key(snapshot)
        if self._stop_event.is_set():
            return _failure("closed")
        try:
            with self._locked():
                state = self._read_state_locked()
                current, error = self._runtime_for_epoch(epoch)
                if error:
                    return _failure(error)
                if current is not runtime:
                    return _failure("epoch_changed")
                job = self._find_job(state, epoch, task_id, instance_id)
                if (
                    job is None
                    or not job["enabled"]
                    or job["pending_notice"] is None
                    or job["pending_notice"]["event_id"] != notice["event_id"]
                ):
                    return {"ok": True, "stale": True}
                if self._stop_event.is_set():
                    return _failure("closed")
                result = runtime.enqueue_event(
                    notice["event_id"], task_id, instance_id, notice["kind"], notice["summary"]
                )
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            self._record_enqueue_error(snapshot, "state_io_error")
            return _failure("state_io_error")
        if type(result) is not dict or result.get("ok") is not True:
            error = result.get("error") if type(result) is dict else None
            if type(error) is not str or error not in _ERRORS:
                error = "state_io_error"
            self._record_enqueue_error(snapshot, error)
            return _failure(error)
        if self._stop_event.is_set():
            return _failure("closed")
        current, error = self._runtime_for_epoch(epoch)
        if error:
            return _failure(error)
        if current is not runtime:
            return _failure("epoch_changed")
        try:
            with self._locked():
                state = self._read_state_locked()
                current, error = self._runtime_for_epoch(epoch)
                if error:
                    return _failure(error)
                job = self._find_job(state, epoch, task_id, instance_id)
                if job is None or job["pending_notice"] is None or job["pending_notice"]["event_id"] != notice["event_id"]:
                    return {"ok": True, "stale": True}
                if not job["enabled"] or self._stop_event.is_set():
                    return _failure("closed" if self._stop_event.is_set() else "paused")
                for field, value in notice["after"].items():
                    if field == "paused":
                        job[field] = job[field] or value
                    else:
                        job[field] = value
                job["pending_notice"] = None
                self._write_state_locked(state)
            return {"ok": True}
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")

    def _record_enqueue_error(self, snapshot, error):
        if self._stop_event.is_set():
            return
        epoch, task_id, instance_id = self._job_key(snapshot)
        try:
            with self._locked():
                state = self._read_state_locked()
                current, runtime_error = self._runtime_for_epoch(epoch)
                if runtime_error:
                    return
                job = self._find_job(state, epoch, task_id, instance_id)
                if job is not None and job["enabled"] and job["pending_notice"] is not None:
                    job["last_error"] = error
                    self._write_state_locked(state)
        except Exception:
            return

    def _select_job(self, epoch):
        with self._transaction() as state:
            available = [
                job for job in state["jobs"]
                if job["epoch"] == epoch and job["enabled"] and not job["paused"] and job["pending_notice"] is None
            ]
            if not available:
                return None
            position = self._cursor % len(available)
            snapshot = copy.deepcopy(available[position])
            self._cursor = (position + 1) % len(available)
            return snapshot

    def _observation_after(self, job, agent, error):
        after = self._cursor_from_job(job)
        state_seq = agent.get("state_change_seq") if agent is not None else job["last_state_change_seq"]
        completion_seq = agent.get("completion_seq") if agent is not None else job["last_completion_seq"]
        status = agent.get("agent_status") if agent is not None else job["last_agent_status"]

        if error is not None:
            after["last_error"] = error
            if error in {"identity_mismatch", "identity_unknown", "sequence_regressed", "sequence_inconsistent"}:
                after["paused"] = True
            if job["last_error"] == error:
                return after, None
            kind = "removed" if error == "not_found" else "unknown"
            notice = self._notice(job, kind, error, status, state_seq, completion_seq, after)
            return after, notice

        state_seq = agent["state_change_seq"]
        completion_seq = agent["completion_seq"]
        status = agent["agent_status"]
        if state_seq is None:
            after["last_error"] = "sequence_unknown"
            if job["last_error"] == "sequence_unknown":
                return after, None
            return after, self._notice(
                job, "unknown", "sequence_unknown", status, state_seq, completion_seq, after
            )

        prior_state_seq = job["last_state_change_seq"]
        prior_completion = job["last_completion_seq"]
        if state_seq < prior_state_seq or (completion_seq is not None and completion_seq < prior_completion):
            after["last_error"] = "sequence_regressed"
            after["paused"] = True
            return after, self._notice(
                job, "unknown", "sequence_regressed", status, state_seq, completion_seq, after
            )
        if completion_seq is not None and completion_seq > state_seq:
            after["last_error"] = "sequence_inconsistent"
            after["paused"] = True
            return after, self._notice(
                job, "unknown", "sequence_inconsistent", status, state_seq, completion_seq, after
            )

        after["last_state_change_seq"] = state_seq
        after["last_agent_status"] = status
        after["last_error"] = None
        if completion_seq is not None:
            after["last_completion_seq"] = max(prior_completion, completion_seq)

        if status in {"idle", "done"}:
            if completion_seq is None:
                after["last_error"] = "sequence_unknown"
                if job["last_error"] == "sequence_unknown":
                    return after, None
                return after, self._notice(
                    job, "unknown", "sequence_unknown", status, state_seq, completion_seq, after
                )
            if completion_seq == state_seq and completion_seq > prior_completion:
                return after, self._notice(job, "idle", None, status, state_seq, completion_seq, after)
        elif status == "blocked" and state_seq != job["last_blocked_seq"]:
            after["last_blocked_seq"] = state_seq
            return after, self._notice(job, "blocked", None, status, state_seq, completion_seq, after)
        return after, None

    def _store_observation(self, snapshot, agent, error):
        epoch, task_id, instance_id = self._job_key(snapshot)
        after, notice = self._observation_after(snapshot, agent, error)
        if self._stop_event.is_set():
            return _failure("closed")
        runtime, runtime_error = self._runtime_for_epoch(epoch)
        if runtime_error:
            return _failure(runtime_error)
        try:
            with self._locked():
                state = self._read_state_locked()
                current_runtime, runtime_error = self._runtime_for_epoch(epoch)
                if runtime_error:
                    return _failure(runtime_error)
                if self._stop_event.is_set():
                    return _failure("closed")
                current = self._find_job(state, epoch, task_id, instance_id)
                if current is None or current != snapshot:
                    return {"ok": True}
                if notice is not None:
                    current["pending_notice"] = notice
                else:
                    for field, value in after.items():
                        current[field] = value
                self._write_state_locked(state)
            if notice is not None:
                result = self._commit_pending({**copy.deepcopy(snapshot), "pending_notice": notice}, runtime)
                if result.get("ok") is not True:
                    return result
            return {"ok": True}
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")

    def _poll_one(self, epoch):
        if self._stop_event.is_set():
            return _failure("closed")
        try:
            snapshot = self._select_job(epoch)
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")
        if snapshot is None:
            return {"ok": True}
        if self._stop_event.is_set():
            return _failure("closed")
        try:
            result, error = self._read_result(snapshot["binding"])
        except Exception:
            result, error = None, "unavailable"
        if self._stop_event.is_set():
            return _failure("closed")
        current, runtime_error = self._runtime_for_epoch(epoch)
        if runtime_error:
            return _failure(runtime_error)
        return self._store_observation(snapshot, result, error)

    def _flush(self, epoch):
        if self._stop_event.is_set():
            return _failure("closed")
        try:
            permitted = self.can_deliver()
        except Exception:
            return _failure("unavailable")
        if permitted is not True:
            return {"ok": True}
        runtime, error = self._runtime_for_epoch(epoch)
        if error:
            return _failure(error)
        if self._stop_event.is_set():
            return _failure("closed")
        try:
            result = runtime.flush_once()
        except Exception:
            return _failure("delivery_unknown")
        if type(result) is not dict:
            return _failure("delivery_unknown")
        if result.get("ok") is not True:
            error = result.get("error")
            return _failure(error if type(error) is str and error in _ERRORS else "state_io_error")
        return {"ok": True}

    def _tick(self):
        if self._stop_event.is_set():
            return _failure("closed")
        runtime, epoch, error = self._active_runtime()
        if error:
            return _failure(error)
        operation_error = None
        try:
            pending = self._pending_snapshots(epoch)
        except _StateFailure as failure:
            pending = []
            operation_error = _failure(failure.code)
        except Exception:
            pending = []
            operation_error = _failure("state_io_error")
        if operation_error is None:
            for snapshot in pending:
                if self._stop_event.is_set():
                    return _failure("closed")
                current, runtime_error = self._runtime_for_epoch(epoch)
                if runtime_error:
                    operation_error = _failure(runtime_error)
                    break
                if current is not runtime:
                    operation_error = _failure("epoch_changed")
                    break
                result = self._commit_pending(snapshot, runtime)
                if result.get("ok") is not True:
                    operation_error = result
                    break

        if operation_error is None and not self._stop_event.is_set():
            current, runtime_error = self._runtime_for_epoch(epoch)
            if runtime_error:
                operation_error = _failure(runtime_error)
            elif current is not runtime:
                operation_error = _failure("epoch_changed")
            else:
                operation_error = self._poll_one(epoch)
        if self._stop_event.is_set():
            return _failure("closed")
        current, runtime_error = self._runtime_for_epoch(epoch)
        if runtime_error:
            return _failure(runtime_error)
        if current is not runtime:
            return _failure("epoch_changed")
        flush_result = self._flush(epoch)
        if flush_result.get("ok") is not True:
            return flush_result
        if operation_error is not None and operation_error.get("ok") is not True:
            return operation_error
        return {"ok": True}

    def tick(self):
        try:
            return self._tick()
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")

    def _thread_main(self):
        while not self._stop_event.is_set():
            try:
                result = self.tick()
            except Exception:
                result = _failure("state_io_error")
            if type(result) is not dict:
                result = _failure("state_io_error")
            if result.get("ok") is True:
                with self._lifecycle_lock:
                    if not self._stop_event.is_set():
                        self._last_error = None
            else:
                if not self._stop_event.is_set():
                    self._set_last_error(result.get("error", "state_io_error"))
            self._stop_event.wait(self.interval)

    def _set_last_error(self, error):
        if type(error) is not str or error not in _ERRORS:
            error = "state_io_error"
        with self._lifecycle_lock:
            self._last_error = error

    def start(self):
        with self._lifecycle_lock:
            if self._stop_event.is_set():
                return _failure("closed")
            if self._thread is not None:
                if self._thread.is_alive():
                    return {"ok": True, "running": True}
                return _failure("closed")
            thread = threading.Thread(target=self._thread_main, name="relay-observer", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._last_error = "state_io_error"
                return _failure("state_io_error")
            return {"ok": True, "running": True}

    def stop(self):
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
            if thread is None:
                return {"ok": True, "stopped": True}
            if threading.current_thread() is thread:
                self._last_error = "shutdown_pending"
                return _failure("shutdown_pending")

        try:
            thread.join(12)
            alive = thread.is_alive()
        except Exception:
            alive = True
        if alive:
            self._set_last_error("shutdown_pending")
            return _failure("shutdown_pending")
        return {"ok": True, "stopped": True}

    def status(self, epoch=None):
        if epoch is None:
            with self._lifecycle_lock:
                running = self._thread is not None and self._thread.is_alive()
                error = self._last_error
            return {"ok": True, "running": running, "last_error": error, "jobs": [], "orphaned_count": 0}
        if type(epoch) is not str or _EPOCH_PATTERN.fullmatch(epoch) is None:
            return _failure("invalid_input")
        try:
            with self._transaction() as state:
                jobs = [self._public_job(job) for job in state["jobs"] if job["epoch"] == epoch]
                orphaned = {
                    self._job_key(job)
                    for job in state["jobs"]
                    if job["epoch"] != epoch or (not job["enabled"] and job["pending_notice"] is not None)
                }
            with self._lifecycle_lock:
                running = self._thread is not None and self._thread.is_alive()
                error = self._last_error
            return {
                "ok": True,
                "running": running,
                "last_error": error,
                "jobs": jobs,
                "orphaned_count": len(orphaned),
            }
        except _StateFailure as failure:
            return _failure(failure.code)
        except Exception:
            return _failure("state_io_error")
