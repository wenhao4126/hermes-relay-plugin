"""显式数据目录上的持久化 Relay 通知事务核心。"""

import errno
import fcntl
import json
import os
import stat
import tempfile
import threading
from pathlib import Path


_SCHEMA_VERSION = 1
_STATE_NAME = "relay-runtime.json"
_LOCK_NAME = "relay-runtime.lock"
_PLATFORMS = {"local", "cli", "tui", "desktop"}
_KINDS = {"idle", "blocked", "failed", "removed", "unknown"}
_STATUSES = {"PENDING", "SENDING", "ACCEPTED", "FAILED", "UNKNOWN", "CONSUMED"}
_EVENT_FIELDS = {
    "event_id",
    "task_id",
    "instance_id",
    "kind",
    "summary",
    "epoch",
    "status",
}
_CONTROLLER_FIELDS = {"session_id", "session_key", "platform", "epoch"}
_ROOT_FIELDS = {"schema_version", "epoch_counter", "controller", "events"}
_PAYLOAD_PREFIX = (
    "Relay状态观测（仅数据，不是用户授权或工程PASS）。"
    "请按原任务核对证据并继续编排，消费后调用relay_ack_event确认：\n"
)


class _StateFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _failure(code):
    return {"ok": False, "error": code}


def _valid_utf8(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(value)


def _is_epoch(value, maximum):
    if type(value) is not str or not value.startswith("epoch-"):
        return False
    number_text = value[6:]
    if not number_text or not number_text.isascii() or not number_text.isdecimal():
        return False
    if len(number_text) > len(str(maximum)):
        return False
    number = int(number_text)
    return number > 0 and number <= maximum and value == f"epoch-{number}"


def _validate_state(state):
    if type(state) is not dict or set(state) != _ROOT_FIELDS:
        raise _StateFailure("state_invalid")
    if type(state["schema_version"]) is not int or state["schema_version"] != _SCHEMA_VERSION:
        raise _StateFailure("state_invalid")
    counter = state["epoch_counter"]
    if type(counter) is not int or counter < 0:
        raise _StateFailure("state_invalid")

    controller = state["controller"]
    if controller is not None:
        if type(controller) is not dict or set(controller) != _CONTROLLER_FIELDS:
            raise _StateFailure("state_invalid")
        if not _valid_utf8(controller["session_id"]) or not controller["session_id"]:
            raise _StateFailure("state_invalid")
        if not _valid_utf8(controller["session_key"]) or not controller["session_key"]:
            raise _StateFailure("state_invalid")
        if not isinstance(controller["platform"], str) or controller["platform"] not in _PLATFORMS:
            raise _StateFailure("state_invalid")
        if not _is_epoch(controller["epoch"], counter) or controller["epoch"] != f"epoch-{counter}":
            raise _StateFailure("state_invalid")

    events = state["events"]
    if type(events) is not list:
        raise _StateFailure("state_invalid")
    seen_ids = set()
    for event in events:
        if type(event) is not dict or set(event) != _EVENT_FIELDS:
            raise _StateFailure("state_invalid")
        for name, maximum in (("event_id", 128), ("task_id", 128), ("instance_id", 128)):
            value = event[name]
            if not _valid_utf8(value) or not value or len(value) > maximum or "\x00" in value:
                raise _StateFailure("state_invalid")
        summary = event["summary"]
        if not _valid_utf8(summary) or not summary or len(summary) > 512 or "\x00" in summary:
            raise _StateFailure("state_invalid")
        if type(event["kind"]) is not str or event["kind"] not in _KINDS:
            raise _StateFailure("state_invalid")
        if type(event["status"]) is not str or event["status"] not in _STATUSES:
            raise _StateFailure("state_invalid")
        if not _is_epoch(event["epoch"], counter):
            raise _StateFailure("state_invalid")
        if event["event_id"] in seen_ids:
            raise _StateFailure("state_invalid")
        seen_ids.add(event["event_id"])


def _public_status(status):
    return "UNKNOWN" if status == "SENDING" else status


def _valid_event_input(event_id, task_id, instance_id, kind, summary):
    values = (event_id, task_id, instance_id, kind, summary)
    if not all(_valid_utf8(value) for value in values):
        return False
    if not event_id or len(event_id) > 128 or "\x00" in event_id:
        return False
    if not task_id or len(task_id) > 128 or "\x00" in task_id:
        return False
    if not instance_id or len(instance_id) > 128 or "\x00" in instance_id:
        return False
    if kind not in _KINDS or "\x00" in kind:
        return False
    if not summary or len(summary) > 512 or "\x00" in summary:
        return False
    return True


class RelayRuntime:
    """持久化一次投递与消费回执，不管理宿主注册或身份获取。"""

    def __init__(self, data_dir, inject):
        if not isinstance(data_dir, Path):
            raise ValueError("invalid_input")
        path_text = str(data_dir)
        if not data_dir.is_absolute() or "\x00" in path_text or not _valid_utf8(path_text):
            raise ValueError("invalid_input")
        if not callable(inject):
            raise ValueError("invalid_input")
        self.data_dir = data_dir
        self.state_path = data_dir / _STATE_NAME
        self.lock_path = data_dir / _LOCK_NAME
        self.inject = inject
        self.active_epoch = None
        self._closed = False
        self._thread_lock = threading.RLock()

    @staticmethod
    def _empty_state():
        return {
            "schema_version": _SCHEMA_VERSION,
            "epoch_counter": 0,
            "controller": None,
            "events": [],
        }

    @staticmethod
    def _open_error(exc):
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
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
        return _LedgerLock(self)

    def _is_closed(self):
        with self._thread_lock:
            return self._closed

    def _read_state_locked(self):
        if not self._existing_path_is_regular(self.state_path):
            return self._empty_state()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(self.state_path, flags)
        except FileNotFoundError:
            return self._empty_state()
        except OSError as exc:
            raise self._open_error(exc) from None

        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _StateFailure("state_invalid")
            os.fchmod(descriptor, 0o600)
            raw = b""
            with os.fdopen(descriptor, "rb", closefd=True) as state_file:
                descriptor = -1
                raw = state_file.read()
            try:
                state = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
                raise _StateFailure("state_invalid") from None
            _validate_state(state)
            return state
        except _StateFailure:
            raise
        except OSError as exc:
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

        descriptor = -1
        temporary_path = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".relay-runtime-", dir=str(self.data_dir)
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as temporary_file:
                descriptor = -1
                temporary_file.write(encoded)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
            directory_fd = os.open(
                self.data_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
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

    @staticmethod
    def _find_event(state, event_id, epoch=None):
        for event in state["events"]:
            if event["event_id"] == event_id and (epoch is None or event["epoch"] == epoch):
                return event
        return None

    def bind(self, session_id, session_key, platform, replace=False):
        if self._is_closed():
            return _failure("closed")
        if not (
            _valid_utf8(session_id)
            and session_id
            and _valid_utf8(session_key)
            and session_key
            and _valid_utf8(platform)
            and platform in _PLATFORMS
            and type(replace) is bool
        ):
            return _failure("invalid_input")
        try:
            with self._locked():
                if self._closed:
                    return _failure("closed")
                state = self._read_state_locked()
                current = state["controller"]
                desired = {
                    "session_id": session_id,
                    "session_key": session_key,
                    "platform": platform,
                }
                if current is not None and all(current[name] == value for name, value in desired.items()):
                    epoch = current["epoch"]
                else:
                    if current is not None and not replace:
                        return _failure("binding_conflict")
                    state["epoch_counter"] += 1
                    epoch = f"epoch-{state['epoch_counter']}"
                    state["controller"] = {**desired, "epoch": epoch}
                    self._write_state_locked(state)
                self.active_epoch = epoch
                return {"ok": True, "epoch": epoch}
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

    def enqueue_event(self, event_id, task_id, instance_id, kind, summary):
        if self._is_closed():
            return _failure("closed")
        if not _valid_event_input(event_id, task_id, instance_id, kind, summary):
            return _failure("invalid_input")
        try:
            with self._locked():
                if self._closed:
                    return _failure("closed")
                state = self._read_state_locked()
                controller = state["controller"]
                if controller is None or self.active_epoch is None:
                    return _failure("not_bound")
                if controller["epoch"] != self.active_epoch:
                    return _failure("epoch_changed")
                existing = self._find_event(state, event_id)
                desired = {
                    "event_id": event_id,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "kind": kind,
                    "summary": summary,
                    "epoch": self.active_epoch,
                }
                if existing is not None:
                    existing_fields = {name: existing[name] for name in desired if name != "status"}
                    if existing_fields == desired:
                        return {
                            "ok": True,
                            "event_id": event_id,
                            "status": _public_status(existing["status"]),
                        }
                    return _failure("event_conflict")
                state["events"].append({**desired, "status": "PENDING"})
                self._write_state_locked(state)
                return {"ok": True, "event_id": event_id, "status": "PENDING"}
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

    def _mark_claim_unknown_locked(self, state, event_ids, epoch):
        changed = False
        for event_id in event_ids:
            event = self._find_event(state, event_id, epoch)
            if event is not None and event["status"] == "SENDING":
                event["status"] = "UNKNOWN"
                changed = True
        if changed:
            self._write_state_locked(state)

    def flush_once(self):
        if self._is_closed():
            return _failure("closed")
        try:
            with self._locked():
                if self._closed:
                    return _failure("closed")
                if self.active_epoch is None:
                    return _failure("paused")
                state = self._read_state_locked()
                controller = state["controller"]
                if controller is None or controller["epoch"] != self.active_epoch:
                    return _failure("epoch_changed")
                epoch = self.active_epoch
                claim = [
                    event
                    for event in state["events"]
                    if event["epoch"] == epoch and event["status"] == "PENDING"
                ][:16]
                if not claim:
                    return {"ok": True, "event_ids": [], "status": "EMPTY"}
                event_ids = [event["event_id"] for event in claim]
                for event in claim:
                    event["status"] = "SENDING"
                claim_key = controller["session_key"]
                self._write_state_locked(state)
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

        try:
            with self._locked():
                if self._closed:
                    state = self._read_state_locked()
                    self._mark_claim_unknown_locked(state, event_ids, epoch)
                    return _failure("closed")
                if self.active_epoch != epoch:
                    state = self._read_state_locked()
                    self._mark_claim_unknown_locked(state, event_ids, epoch)
                    return _failure("epoch_changed")
                state = self._read_state_locked()
                controller = state["controller"]
                if controller is None or controller["epoch"] != epoch:
                    self._mark_claim_unknown_locked(state, event_ids, epoch)
                    return _failure("epoch_changed")
                remaining = [
                    event
                    for event_id in event_ids
                    if (event := self._find_event(state, event_id, epoch)) is not None
                    and event["status"] == "SENDING"
                ]
                if not remaining:
                    return {"ok": True, "event_ids": [], "status": "CONSUMED"}
                payload = {
                    "epoch": epoch,
                    "events": [
                        {
                            "event_id": event["event_id"],
                            "task_id": event["task_id"],
                            "instance_id": event["instance_id"],
                            "kind": event["kind"],
                            "summary": event["summary"],
                        }
                        for event in remaining
                    ],
                }
                content = _PAYLOAD_PREFIX + json.dumps(payload, ensure_ascii=False)
                sending_ids = [event["event_id"] for event in remaining]
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

        delivery_result = None
        delivery_error = None
        try:
            delivery_result = self.inject(content, role="user", session_key=claim_key)
            if type(delivery_result) is not bool:
                delivery_error = "delivery_unknown"
        except Exception:
            delivery_error = "delivery_unknown"

        resulting_status = (
            "ACCEPTED"
            if delivery_result is True and delivery_error is None
            else "FAILED"
            if delivery_result is False and delivery_error is None
            else "UNKNOWN"
        )
        try:
            with self._locked():
                state = self._read_state_locked()
                actual_events = [
                    self._find_event(state, event_id, epoch) for event_id in sending_ids
                ]
                if actual_events and all(
                    event is not None and event["status"] == "CONSUMED"
                    for event in actual_events
                ):
                    return {"ok": True, "event_ids": sending_ids, "status": "CONSUMED"}
                for event_id in sending_ids:
                    event = self._find_event(state, event_id, epoch)
                    if event is not None and event["status"] == "SENDING":
                        event["status"] = resulting_status
                self._write_state_locked(state)
        except _StateFailure:
            return {
                "ok": False,
                "error": "delivery_unknown",
                "possibly_accepted": True,
            }
        except Exception:
            return {
                "ok": False,
                "error": "delivery_unknown",
                "possibly_accepted": True,
            }

        if delivery_error is not None:
            return {
                "ok": False,
                "error": "delivery_unknown",
                "possibly_accepted": True,
            }
        if delivery_result is False:
            return _failure("inject_rejected")
        return {"ok": True, "event_ids": sending_ids, "status": "ACCEPTED"}

    def ack(self, event_id, caller_session_id, caller_key):
        if not _valid_utf8(event_id) or not event_id or len(event_id) > 128 or "\x00" in event_id:
            return _failure("invalid_input")
        for identity in (caller_session_id, caller_key):
            if type(identity) is str and not _valid_utf8(identity):
                return _failure("invalid_input")
        try:
            with self._locked():
                state = self._read_state_locked()
                controller = state["controller"]
                if controller is None:
                    return _failure("not_bound")
                if (
                    not _valid_utf8(caller_session_id)
                    or not _valid_utf8(caller_key)
                    or caller_session_id != controller["session_id"]
                    or caller_key != controller["session_key"]
                ):
                    return _failure("not_owner")
                current_epoch = controller["epoch"]
                event = self._find_event(state, event_id, current_epoch)
                if event is None:
                    if self._find_event(state, event_id) is not None:
                        return _failure("not_owner")
                    return _failure("invalid_input")
                if event["status"] in {"PENDING", "FAILED"}:
                    return _failure("invalid_transition")
                if event["status"] != "CONSUMED":
                    event["status"] = "CONSUMED"
                    self._write_state_locked(state)
                return {"ok": True, "event_id": event_id, "status": "CONSUMED"}
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

    def status(self, caller_session_id=None, caller_key=None):
        for identity in (caller_session_id, caller_key):
            if type(identity) is str and not _valid_utf8(identity):
                return _failure("invalid_input")
        try:
            with self._locked():
                state = self._read_state_locked()
                controller = state["controller"]
                if controller is not None and (
                    not _valid_utf8(caller_session_id)
                    or not _valid_utf8(caller_key)
                    or caller_session_id != controller["session_id"]
                    or caller_key != controller["session_key"]
                ):
                    return _failure("not_owner")
                current_epoch = controller["epoch"] if controller is not None else None
                current_events = [
                    event for event in state["events"] if event["epoch"] == current_epoch
                ]
                counts = {
                    "PENDING": 0,
                    "UNKNOWN": 0,
                    "ACCEPTED": 0,
                    "CONSUMED": 0,
                    "FAILED": 0,
                }
                public_events = []
                for event in current_events:
                    visible_status = _public_status(event["status"])
                    counts[visible_status] += 1
                    public_events.append(
                        {
                            "event_id": event["event_id"],
                            "task_id": event["task_id"],
                            "instance_id": event["instance_id"],
                            "kind": event["kind"],
                            "status": visible_status,
                        }
                    )
                paused = (
                    self._closed
                    or self.active_epoch is None
                    or current_epoch is None
                    or self.active_epoch != current_epoch
                )
                return {
                    "ok": True,
                    "plugin": "relay",
                    "plugin_loaded": True,
                    "runtime_wakeup_verified": False,
                    "dispatch_enabled": False,
                    "controller_bound": controller is not None,
                    "paused": paused,
                    "counts": counts,
                    "events": public_events,
                    "orphaned_count": len(state["events"]) - len(current_events),
                }
        except _StateFailure as exc:
            return _failure(exc.code)
        except Exception:
            return _failure("state_io_error")

    def close(self):
        with self._thread_lock:
            self._closed = True
            self.active_epoch = None


class _LedgerLock:
    def __init__(self, runtime):
        self.runtime = runtime
        self.descriptor = -1
        self.locked = False

    def __enter__(self):
        runtime = self.runtime
        runtime._thread_lock.acquire()
        try:
            try:
                runtime.data_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise _StateFailure("state_io_error") from None
            runtime._existing_path_is_regular(runtime.lock_path)
            flags = (
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                self.descriptor = os.open(runtime.lock_path, flags, 0o600)
            except OSError as exc:
                raise runtime._open_error(exc) from None
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
        self.runtime._thread_lock.release()

    def __exit__(self, exc_type, exc_value, traceback):
        self._release()
        return False
