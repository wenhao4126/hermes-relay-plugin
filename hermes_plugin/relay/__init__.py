"""Hermes Relay profile 主控绑定、代理观察与通知消费回执。"""

import json
import re
import threading

from gateway.session_context import bound_session_key, get_session_env

from .dispatcher import Dispatcher
from .creator import Creator
from .gateway_transport import make_gateway_injector
from .gate_tools import register_gate_tools
from .observer import Observer
from .runtime import RelayRuntime


_INVALID_INPUT = {"ok": False, "error": "invalid_input"}
_PLATFORMS = frozenset({"local", "cli", "tui", "desktop"})
_BINDING_FIELDS = (
    "herdr_session", "name", "agent_kind", "workspace_id", "tab_id", "pane_id", "agent_session_id",
)
_AGENT_KINDS = ("codex", "opencode", "claude")
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_DISPATCH_REQUEST_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_DISPATCH_OWNER_CHECKS = frozenset({"model", "permissions", "cwd", "input_ready"})
_DISPATCH_AUTHORITY_ERRORS = frozenset({
    "not_owner", "epoch_changed", "closed", "paused", "state_invalid", "state_io_error",
})
_STATUS_NAMES = (
    ("PENDING", "等待发送"),
    ("UNKNOWN", "发送结果待确认"),
    ("ACCEPTED", "已受理"),
    ("CONSUMED", "已消费"),
    ("FAILED", "发送失败"),
)


def _json_result(result):
    return json.dumps(result, ensure_ascii=False)


def _failure(error):
    return {"ok": False, "error": error}


def _valid_identifier(value):
    if type(value) is not str or not value or len(value) > 128 or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_creation_text(value, limit, *, forbid_leading_dash=False):
    if type(value) is not str or not value or len(value) > limit or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        not any(char in value for char in ("\x00", "\r", "\n"))
        and not (forbid_leading_dash and value.startswith("-"))
    )


def _valid_binding(binding):
    if type(binding) is not dict or set(binding) != set(_BINDING_FIELDS):
        return False
    for value in binding.values():
        if type(value) is not str:
            return False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        if not value.strip() or any(char in value for char in ("\x00", "\r", "\n")):
            return False
    if binding["agent_kind"] not in _AGENT_KINDS:
        return False
    if _NAME_PATTERN.fullmatch(binding["name"]) is None:
        return False
    if binding["herdr_session"].startswith("-"):
        return False
    return True


def _binding_schema():
    properties = {field: {"type": "string", "minLength": 1} for field in _BINDING_FIELDS}
    properties["agent_kind"]["enum"] = list(_AGENT_KINDS)
    return {
        "type": "object",
        "properties": properties,
        "required": list(_BINDING_FIELDS),
        "additionalProperties": False,
    }


def _status_text(result, observer_status, dispatch_status, creator_status):
    bound = result["controller_bound"]
    paused = result["paused"]
    lines = [
        "主控：已绑定" if bound else "主控：未绑定",
        "状态：暂停" if paused else "状态：已激活",
        "通知计数：",
    ]
    for name, description in _STATUS_NAMES:
        lines.append(f"{name}：{result['counts'][name]}（{description}）")

    events = result["events"]
    if events:
        lines.append("当前通知事件（不含摘要与路由密钥）：")
        for event in events:
            public_event = {
                "event_id": event["event_id"],
                "task_id": event["task_id"],
                "instance_id": event["instance_id"],
                "kind": event["kind"],
                "status": event["status"],
            }
            lines.append(json.dumps(public_event, ensure_ascii=False))
    else:
        lines.append("当前通知事件：无")

    lines.append("观察后台：运行中" if observer_status["running"] else "观察后台：已停止")
    if observer_status["last_error"] is not None:
        lines.append(f"观察后台最近错误：{observer_status['last_error']}")
    if observer_status["jobs"]:
        lines.append("当前代次观察任务：")
        for job in observer_status["jobs"]:
            lines.append(json.dumps(job, ensure_ascii=False))
    else:
        lines.append("当前代次观察任务：无")
    lines.append(f"旧代次或已退役意图数：{observer_status['orphaned_count']}")
    lines.append("派发记录：")
    if dispatch_status["requests"]:
        for request in dispatch_status["requests"]:
            lines.append(json.dumps(request, ensure_ascii=False))
    else:
        lines.append("当前代次派发记录：无")
    lines.append(f"旧代次派发记录数：{dispatch_status['orphaned_count']}")
    lines.append("创建空CLI记录：")
    if creator_status["requests"]:
        for request in creator_status["requests"]:
            lines.append(json.dumps(request, ensure_ascii=False))
    else:
        lines.append("当前代次创建记录：无")
    lines.append(f"旧代次创建记录数：{creator_status['orphaned_count']}")
    lines.append("提示：经典界面不会自动展示后台回合；创建结果需原主控核实模型与权限。")
    return "\n".join(lines)


def register(ctx):
    """为当前 profile 捕获路径与 Gateway 接线，并注册状态、回执和观察入口。"""
    data_dir = ctx.state.data_dir
    inject = make_gateway_injector(ctx)
    runtime = RelayRuntime(data_dir, inject)
    runtime_lock = threading.RLock()
    unloaded = False
    bound_epoch = None

    try:
        delivery_manager = ctx._manager
        permission_getter = ctx._gateway_injection_allowed
    except Exception:
        delivery_manager = None
        permission_getter = None

    def can_deliver():
        if delivery_manager is None or not callable(permission_getter):
            return False
        try:
            ready = delivery_manager.has_gateway_message_injector
            if type(ready) is not bool or ready is not True:
                return False
            return permission_getter() is True
        except Exception:
            return False

    def get_runtime():
        with runtime_lock:
            return runtime

    try:
        observer = Observer(data_dir, get_runtime, can_deliver=can_deliver)
    except BaseException:
        runtime.close()
        raise

    try:
        dispatcher = Dispatcher(data_dir, get_runtime, observer)
    except BaseException:
        runtime.close()
        observer.stop()
        raise

    try:
        creator = Creator(data_dir, get_runtime)
    except BaseException:
        runtime.close()
        dispatcher.close()
        observer.stop()
        raise

    def close_current_runtime():
        nonlocal unloaded
        with runtime_lock:
            unloaded = True
            runtime.close()
        dispatcher.close()
        creator.close()
        observer.stop()

    def caller_identity():
        try:
            caller_key = bound_session_key()
            caller_session_id = get_session_env("HERMES_SESSION_ID", "")
            return caller_session_id, caller_key
        except Exception:
            return None, None

    def current_runtime():
        with runtime_lock:
            return runtime, bound_epoch, unloaded

    def read_current_status(caller_session_id, caller_key):
        with runtime_lock:
            selected = runtime
        result = selected.status(caller_session_id, caller_key)
        with runtime_lock:
            if runtime is not selected:
                return None, None, _failure("epoch_changed")
            return selected, bound_epoch, result

    def relay_plugin_status(args: dict, **_kwargs) -> str:
        if type(args) is not dict or args:
            return _json_result(_INVALID_INPUT)
        caller_session_id, caller_key = caller_identity()
        selected, epoch, result = read_current_status(caller_session_id, caller_key)
        if result is None:
            return _json_result(_failure("epoch_changed"))
        if result.get("ok") is not True:
            return _json_result(result)
        observer_result = observer.status(epoch if result["controller_bound"] else None)
        if observer_result.get("ok") is not True:
            return _json_result(observer_result)
        dispatch_result = dispatcher.status(epoch if result["controller_bound"] else None)
        if dispatch_result.get("ok") is not True:
            return _json_result(dispatch_result)
        creator_result = creator.status(epoch if result["controller_bound"] else None)
        if creator_result.get("ok") is not True:
            return _json_result(creator_result)
        with runtime_lock:
            if runtime is not selected:
                return _json_result(_failure("epoch_changed"))
            is_unloaded = unloaded
        result["dispatch_enabled"] = bool(
            result["controller_bound"] and not result["paused"] and not is_unloaded
        )
        result["observer"] = observer_result
        result["dispatch"] = dispatch_result
        result["creation"] = creator_result
        return _json_result(result)

    def relay_ack_event(args: dict, **_kwargs) -> str:
        if type(args) is not dict or set(args) != {"event_id"}:
            return _json_result(_INVALID_INPUT)
        event_id = args["event_id"]
        if not isinstance(event_id, str) or not event_id:
            return _json_result(_INVALID_INPUT)
        caller_session_id, caller_key = caller_identity()
        selected, _epoch, _is_unloaded = current_runtime()
        return _json_result(selected.ack(event_id, caller_session_id, caller_key))

    def authorized_observer_epoch():
        caller_session_id, caller_key = caller_identity()
        selected, _stored_epoch, is_unloaded = current_runtime()
        result = selected.status(caller_session_id, caller_key)
        if result.get("ok") is not True:
            return None, result
        if not result["controller_bound"]:
            return None, _failure("not_bound")
        if result["paused"]:
            return None, _failure("closed" if is_unloaded else "paused")
        with runtime_lock:
            if unloaded:
                return None, _failure("closed")
            if runtime is not selected:
                return None, _failure("epoch_changed")
            epoch = selected.active_epoch
            if not epoch:
                return None, _failure("paused")
        return epoch, result

    def dispatch_authority_check(caller_session_id, caller_key, selected, epoch):
        """捕获一次可信调用者，并在每个派发阶段重新核查磁盘 owner。"""
        def check():
            with runtime_lock:
                current = runtime
            if current is None:
                return "paused"
            try:
                result = current.status(caller_session_id, caller_key)
            except Exception:
                return "state_io_error"
            if type(result) is not dict:
                return "state_io_error"
            with runtime_lock:
                if unloaded:
                    return "closed"
                pointer_matches = runtime is selected
                current_bound_epoch = bound_epoch
                current_epoch = selected.active_epoch
            if result.get("ok") is not True:
                error = result.get("error")
                return error if type(error) is str and error in _DISPATCH_AUTHORITY_ERRORS else "not_owner"
            if type(result.get("controller_bound")) is not bool:
                return "state_invalid"
            if result["controller_bound"] is not True:
                return "not_owner"
            if not pointer_matches or current_bound_epoch != epoch:
                return "epoch_changed"
            if type(result.get("paused")) is not bool:
                return "state_invalid"
            if result.get("paused") is True:
                if current_epoch == epoch:
                    return "epoch_changed"
                return "paused"
            if current_epoch != epoch:
                return "paused" if current_epoch is None else "epoch_changed"
            return None

        return check

    def authorized_dispatch_context():
        caller_session_id, caller_key = caller_identity()
        selected, _stored_epoch, is_unloaded = current_runtime()
        if is_unloaded:
            return None, _failure("closed"), None
        try:
            result = selected.status(caller_session_id, caller_key)
        except Exception:
            return None, _failure("state_io_error"), None
        if type(result) is not dict:
            return None, _failure("state_io_error"), None
        if result.get("ok") is not True:
            return None, result, None
        if not result["controller_bound"]:
            return None, _failure("not_bound"), None
        if result["paused"]:
            return None, _failure("paused"), None
        with runtime_lock:
            if unloaded:
                return None, _failure("closed"), None
            if runtime is not selected:
                return None, _failure("epoch_changed"), None
            epoch = selected.active_epoch
            if not epoch:
                return None, _failure("paused"), None
        authority_check = dispatch_authority_check(caller_session_id, caller_key, selected, epoch)
        return epoch, result, authority_check

    def relay_watch_task(args: dict, **_kwargs) -> str:
        if (
            type(args) is not dict
            or set(args) != {"task_id", "instance_id", "binding"}
            or not _valid_identifier(args["task_id"])
            or not _valid_identifier(args["instance_id"])
            or not _valid_binding(args["binding"])
        ):
            return _json_result(_INVALID_INPUT)
        epoch, result = authorized_observer_epoch()
        if epoch is None:
            return _json_result(result)
        result = observer.track(args["task_id"], args["instance_id"], args["binding"], epoch=epoch)
        if result.get("ok") is True:
            observer.start()
        return _json_result(result)

    def relay_unwatch_task(args: dict, **_kwargs) -> str:
        if (
            type(args) is not dict
            or set(args) != {"task_id", "instance_id"}
            or not _valid_identifier(args["task_id"])
            or not _valid_identifier(args["instance_id"])
        ):
            return _json_result(_INVALID_INPUT)
        epoch, result = authorized_observer_epoch()
        if epoch is None:
            return _json_result(result)
        return _json_result(observer.untrack(args["task_id"], args["instance_id"], epoch=epoch))

    def relay_dispatch_task(args: dict, **_kwargs) -> str:
        required = {"request_id", "binding", "task", "jev_model", "owner_checks"}
        if type(args) is not dict or set(args) != required:
            return _json_result(_INVALID_INPUT)
        request_id = args["request_id"]
        owner_checks = args["owner_checks"]
        if (
            type(request_id) is not str
            or _DISPATCH_REQUEST_PATTERN.fullmatch(request_id) is None
            or request_id.startswith("-")
            or not _valid_binding(args["binding"])
            or type(args["task"]) is not dict
            or type(args["jev_model"]) is not str
            or not args["jev_model"]
            or type(owner_checks) is not dict
            or set(owner_checks) != _DISPATCH_OWNER_CHECKS
            or any(type(value) is not bool for value in owner_checks.values())
        ):
            return _json_result(_INVALID_INPUT)
        epoch, result, authority_check = authorized_dispatch_context()
        if epoch is None:
            return _json_result(result)
        dispatch_result = dispatcher.dispatch(
            epoch=epoch,
            request_id=request_id,
            binding=args["binding"],
            task=args["task"],
            jev_model=args["jev_model"],
            owner_checks=owner_checks,
            authority_check=authority_check,
        )
        if type(dispatch_result) is dict and dispatch_result.get("status") in {"SUBMITTED", "UNKNOWN"}:
            observed = observer.status(epoch)
            if observed.get("ok") is True and any(
                item.get("task_id") == args["task"].get("task_id") and item.get("enabled") is True
                for item in observed["jobs"]
            ):
                observer.start()
        return _json_result(dispatch_result)

    def relay_create_agent(args: dict, **_kwargs) -> str:
        required = {
            "request_id", "herdr_session", "workspace_id", "name", "role", "reasoning_effort", "task", "jev_model",
        }
        if type(args) is not dict or set(args) != required:
            return _json_result(_INVALID_INPUT)
        if (
            type(args["request_id"]) is not str or _DISPATCH_REQUEST_PATTERN.fullmatch(args["request_id"]) is None
            or args["request_id"].startswith("-")
            or not _valid_creation_text(args["herdr_session"], 256, forbid_leading_dash=True)
            or not _valid_creation_text(args["workspace_id"], 256, forbid_leading_dash=True)
            or type(args["name"]) is not str or _NAME_PATTERN.fullmatch(args["name"]) is None
            or type(args["role"]) is not str or args["role"] not in {"worker", "fixer", "tester", "reviewer", "auditor"}
            or type(args["reasoning_effort"]) is not str or args["reasoning_effort"] not in {"high", "max"}
            or (args["role"] in {"tester", "reviewer", "auditor"} and args["reasoning_effort"] != "high")
            or type(args["task"]) is not dict
            or not _valid_creation_text(args["jev_model"], 128)
        ):
            return _json_result(_INVALID_INPUT)
        epoch, result, authority_check = authorized_dispatch_context()
        if epoch is None:
            return _json_result(result)
        return _json_result(creator.create(
            epoch=epoch,
            request_id=args["request_id"],
            herdr_session=args["herdr_session"],
            workspace_id=args["workspace_id"],
            name=args["name"],
            role=args["role"],
            reasoning_effort=args["reasoning_effort"],
            task=args["task"],
            jev_model=args["jev_model"],
            authority_check=authority_check,
        ))

    def relay_refresh_agent(args: dict, **_kwargs) -> str:
        if type(args) is not dict or set(args) != {"request_id"}:
            return _json_result(_INVALID_INPUT)
        request_id = args["request_id"]
        if type(request_id) is not str or _DISPATCH_REQUEST_PATTERN.fullmatch(request_id) is None or request_id.startswith("-"):
            return _json_result(_INVALID_INPUT)
        epoch, result, authority_check = authorized_dispatch_context()
        if epoch is None:
            return _json_result(result)
        return _json_result(creator.refresh(epoch=epoch, request_id=request_id, authority_check=authority_check))

    def relaybind(
        raw_args,
        *,
        session_id=None,
        session_key=None,
        platform=None,
        source=None,
    ):
        nonlocal runtime, bound_epoch
        if not isinstance(raw_args, str):
            return "绑定失败：invalid_input"
        option = raw_args.strip()
        if option not in {"", "--replace"}:
            return "绑定失败：invalid_input"
        replace = option == "--replace"

        if not isinstance(platform, str) or platform not in _PLATFORMS:
            return "绑定失败：invalid_input"
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(session_key, str)
            or not session_key
            or source is None
        ):
            return "绑定失败：not_owner"

        with runtime_lock:
            if unloaded:
                return "绑定失败：closed"
            candidate = RelayRuntime(data_dir, inject)
            try:
                result = candidate.bind(
                    session_id,
                    session_key,
                    platform,
                    replace=replace,
                )
            except BaseException:
                candidate.close()
                raise
            if not result["ok"]:
                candidate.close()
                return f"绑定失败：{result['error']}"

            previous = runtime
            runtime = candidate
            bound_epoch = result["epoch"]
            active_epoch = bound_epoch
            previous.close()

        observer.start()
        return f"主控已绑定\n绑定代次：{active_epoch}\n支持受控创建空CLI并在主控核实后派发。"

    def relaystatus(
        raw_args,
        *,
        session_id=None,
        session_key=None,
        platform=None,
        source=None,
    ):
        if not isinstance(raw_args, str) or raw_args.strip():
            return "查询失败：invalid_input"
        if not isinstance(platform, str) or platform not in _PLATFORMS:
            return "查询失败：invalid_input"
        selected, epoch, result = read_current_status(session_id, session_key)
        if result is None:
            return "查询失败：epoch_changed"
        if not result["ok"]:
            return f"查询失败：{result['error']}"
        if result["controller_bound"] and source is None:
            return "查询失败：not_owner"
        observer_result = observer.status(epoch if result["controller_bound"] else None)
        if observer_result.get("ok") is not True:
            return f"查询失败：{observer_result['error']}"
        dispatch_result = dispatcher.status(epoch if result["controller_bound"] else None)
        if dispatch_result.get("ok") is not True:
            return f"查询失败：{dispatch_result['error']}"
        with runtime_lock:
            if runtime is not selected:
                return "查询失败：epoch_changed"
            is_unloaded = unloaded
        result["dispatch_enabled"] = bool(
            result["controller_bound"] and not result["paused"] and not is_unloaded
        )
        creator_result = creator.status(epoch if result["controller_bound"] else None)
        if creator_result.get("ok") is not True:
            return f"查询失败：{creator_result['error']}"
        return _status_text(result, observer_result, dispatch_result, creator_result)

    try:
        ctx.on_unload(close_current_runtime)
        status_registration = ctx.register_tool(
            name="relay_plugin_status",
            toolset="relay",
            schema={
                "name": "relay_plugin_status",
                "description": "查询本profile主控通知状态与观察进度。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            handler=relay_plugin_status,
        )
        if status_registration is None:
            raise RuntimeError("tool_registration_failed")

        ack_registration = ctx.register_tool(
            name="relay_ack_event",
            toolset="relay",
            schema={
                "name": "relay_ack_event",
                "description": "原绑定主控确认已消费指定通知；不代表工程验收通过。",
                "parameters": {
                    "type": "object",
                    "properties": {"event_id": {"type": "string", "minLength": 1}},
                    "required": ["event_id"],
                    "additionalProperties": False,
                },
            },
            handler=relay_ack_event,
        )
        if ack_registration is None:
            raise RuntimeError("tool_registration_failed")

        watch_registration = ctx.register_tool(
            name="relay_watch_task",
            toolset="relay",
            schema={
                "name": "relay_watch_task",
                "description": "登记一个已存在的 Herdr 代理任务供只读观察。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1},
                        "instance_id": {"type": "string", "minLength": 1},
                        "binding": _binding_schema(),
                    },
                    "required": ["task_id", "instance_id", "binding"],
                    "additionalProperties": False,
                },
            },
            handler=relay_watch_task,
        )
        if watch_registration is None:
            raise RuntimeError("tool_registration_failed")

        unwatch_registration = ctx.register_tool(
            name="relay_unwatch_task",
            toolset="relay",
            schema={
                "name": "relay_unwatch_task",
                "description": "停止观察一个已登记任务并保留其记录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1},
                        "instance_id": {"type": "string", "minLength": 1},
                    },
                    "required": ["task_id", "instance_id"],
                    "additionalProperties": False,
                },
            },
            handler=relay_unwatch_task,
        )
        if unwatch_registration is None:
            raise RuntimeError("tool_registration_failed")

        dispatch_registration = ctx.register_tool(
            name="relay_dispatch_task",
            toolset="relay",
            schema={
                "name": "relay_dispatch_task",
                "description": "向原主控核实并获同profile门禁批准的已有代理单次提交任务。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string", "minLength": 1, "maxLength": 96},
                        "binding": _binding_schema(),
                        "task": {"type": "object"},
                        "jev_model": {"type": "string", "minLength": 1},
                        "owner_checks": {
                            "type": "object",
                            "properties": {
                                name: {"type": "boolean"}
                                for name in sorted(_DISPATCH_OWNER_CHECKS)
                            },
                            "required": sorted(_DISPATCH_OWNER_CHECKS),
                            "additionalProperties": False,
                        },
                    },
                    "required": ["request_id", "binding", "task", "jev_model", "owner_checks"],
                    "additionalProperties": False,
                },
            },
            handler=relay_dispatch_task,
        )
        if dispatch_registration is None:
            raise RuntimeError("tool_registration_failed")

        register_gate_tools(ctx, data_dir, authorized_dispatch_context)

        create_registration = ctx.register_tool(
            name="relay_create_agent",
            toolset="relay",
            schema={
                "name": "relay_create_agent",
                "description": "为同profile已批准任务创建原生空CLI，结果需原主控核实。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string", "minLength": 1, "maxLength": 96},
                        "herdr_session": {"type": "string", "minLength": 1, "maxLength": 256},
                        "workspace_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,31}$"},
                        "role": {"type": "string", "enum": ["worker", "fixer", "tester", "reviewer", "auditor"]},
                        "reasoning_effort": {"type": "string", "enum": ["high", "max"]},
                        "task": {"type": "object"},
                        "jev_model": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "required": ["request_id", "herdr_session", "workspace_id", "name", "role", "reasoning_effort", "task", "jev_model"],
                    "additionalProperties": False,
                },
            },
            handler=relay_create_agent,
        )
        if create_registration is None:
            raise RuntimeError("tool_registration_failed")

        refresh_registration = ctx.register_tool(
            name="relay_refresh_agent",
            toolset="relay",
            schema={
                "name": "relay_refresh_agent",
                "description": "只读刷新本profile既有创建记录的原代理身份。",
                "parameters": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string", "minLength": 1, "maxLength": 96}},
                    "required": ["request_id"],
                    "additionalProperties": False,
                },
            },
            handler=relay_refresh_agent,
        )
        if refresh_registration is None:
            raise RuntimeError("tool_registration_failed")

        bind_registration = ctx.register_command(
            "relaybind",
            handler=relaybind,
            description="将当前本地会话绑定为 Relay 主控；--replace 会显式替换原主控。",
            args_hint="[--replace]",
        )
        if bind_registration is None:
            raise RuntimeError("command_registration_failed")

        status_command_registration = ctx.register_command(
            "relaystatus",
            handler=relaystatus,
            description="查询当前主控通知、观察任务与受控派发状态。",
        )
        if status_command_registration is None:
            raise RuntimeError("command_registration_failed")
    except BaseException:
        close_current_runtime()
        raise
