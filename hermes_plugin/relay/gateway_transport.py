"""Hermes v0.22.5 的严格 Gateway-only Relay 注入桥接。"""

from pathlib import Path


def _valid_text(value):
    if type(value) is not str or not value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def make_gateway_injector(ctx):
    """捕获插件注册时的 profile 与 SDK 路由能力，返回仅投递到 Gateway 的函数。"""
    try:
        manager = ctx._manager
        home = manager.home_path
        plugin_id = ctx.plugin_id
        permission = ctx._gateway_injection_allowed
        route = manager.inject_gateway_message
    except Exception:
        return lambda content, role="user", *, session_key=None: False

    if (
        not isinstance(home, Path)
        or not home.is_absolute()
        or type(plugin_id) is not str
        or not plugin_id
        or not callable(permission)
        or not callable(route)
    ):
        return lambda content, role="user", *, session_key=None: False

    def inject(content, role="user", *, session_key=None):
        if (
            type(role) is not str
            or role != "user"
            or not _valid_text(content)
            or not _valid_text(session_key)
        ):
            return False

        try:
            ready = manager.has_gateway_message_injector
        except Exception:
            return False
        if type(ready) is not bool or ready is not True:
            return False

        try:
            allowed = permission()
        except Exception:
            return False
        if allowed is not True:
            return False

        result = route(
            session_key=session_key,
            plugin_home=home,
            content=content,
            plugin_id=plugin_id,
        )
        if type(result) is not bool:
            raise RuntimeError("gateway_result_unknown")
        return result

    return inject
