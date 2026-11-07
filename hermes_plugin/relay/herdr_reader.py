"""只读读取已绑定 Herdr agent 的身份与状态。"""

import json
import os
import re
import subprocess


_BINDING_FIELDS = frozenset(
    {
        "herdr_session",
        "name",
        "agent_kind",
        "workspace_id",
        "tab_id",
        "pane_id",
        "agent_session_id",
    }
)
_AGENT_KINDS = frozenset({"codex", "opencode", "claude"})
_AGENT_STATUSES = ("idle", "working", "blocked", "done")
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


def _failure(error):
    return {"ok": False, "error": error}


def _valid_binding(binding):
    if type(binding) is not dict:
        return False
    try:
        if set(binding) != _BINDING_FIELDS:
            return False
        for value in binding.values():
            if not isinstance(value, str) or not str.strip(value):
                return False
            if any(character in value for character in ("\x00", "\r", "\n")):
                return False
    except Exception:
        return False

    if binding["agent_kind"] not in _AGENT_KINDS:
        return False
    if _NAME_PATTERN.fullmatch(binding["name"]) is None:
        return False
    if binding["herdr_session"].startswith("-"):
        return False
    return True


def _child_environment():
    allowed = {"PATH", "HOME", "LANG"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_") or key.startswith("XDG_")
    }
    if "PATH" not in environment:
        environment["PATH"] = os.defpath
    return environment


def _error_from_json(text):
    try:
        response = json.loads(text)
    except Exception:
        return None
    if not isinstance(response, dict):
        return None
    error = response.get("error")
    if not isinstance(error, dict):
        return None
    if error.get("code") == "agent_not_found":
        return "not_found"
    return "unavailable"


def _identity_string(value):
    return isinstance(value, str) and bool(value)


def read_bound_agent(binding, *, runner=None):
    """通过公开 `herdr agent get` 命令校验绑定身份并读取状态。"""
    if not _valid_binding(binding):
        return _failure("invalid_input")

    argv = [
        "herdr",
        "--session",
        binding["herdr_session"],
        "agent",
        "get",
        binding["name"],
    ]
    command_runner = subprocess.run if runner is None else runner
    try:
        process = command_runner(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_child_environment(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return _failure("unavailable")
    except Exception:
        return _failure("unavailable")

    try:
        returncode = process.returncode
        stdout = process.stdout
        stderr = process.stderr
    except Exception:
        return _failure("invalid_response")
    if type(returncode) is not int or not isinstance(stdout, str) or not isinstance(stderr, str):
        return _failure("invalid_response")

    if returncode != 0:
        for stream in (stderr, stdout):
            error = _error_from_json(stream)
            if error is not None:
                return _failure(error)
        return _failure("unavailable")

    try:
        response = json.loads(stdout)
    except Exception:
        return _failure("invalid_response")
    if not isinstance(response, dict):
        return _failure("invalid_response")

    error_object = response.get("error")
    if isinstance(error_object, dict):
        if error_object.get("code") == "agent_not_found":
            return _failure("not_found")
        return _failure("unavailable")

    result = response.get("result")
    if not isinstance(result, dict):
        return _failure("invalid_response")
    agent = result.get("agent")
    if not isinstance(agent, dict):
        return _failure("invalid_response")

    identity_fields = ("name", "agent", "workspace_id", "tab_id", "pane_id")
    observed_identity = {}
    for field in identity_fields:
        value = agent.get(field)
        if not _identity_string(value):
            return _failure("identity_unknown")
        observed_identity[field] = value

    agent_session = agent.get("agent_session")
    if not isinstance(agent_session, dict):
        return _failure("identity_unknown")
    session_kind = agent_session.get("kind")
    session_agent = agent_session.get("agent")
    session_value = agent_session.get("value")
    if (
        session_kind != "id"
        or not _identity_string(session_agent)
        or not _identity_string(session_value)
    ):
        return _failure("identity_unknown")

    expected_identity = {
        "name": binding["name"],
        "agent": binding["agent_kind"],
        "workspace_id": binding["workspace_id"],
        "tab_id": binding["tab_id"],
        "pane_id": binding["pane_id"],
    }
    if any(observed_identity[field] != value for field, value in expected_identity.items()):
        return _failure("identity_mismatch")
    if session_agent != binding["agent_kind"] or session_value != binding["agent_session_id"]:
        return _failure("identity_mismatch")

    agent_status = agent.get("agent_status")
    if agent_status not in _AGENT_STATUSES:
        return _failure("state_unknown")

    interactive_ready = agent.get("interactive_ready")
    if interactive_ready is not None and type(interactive_ready) is not bool:
        return _failure("invalid_response")

    sequence_values = {}
    for field in ("state_change_seq", "completion_seq"):
        value = agent.get(field)
        if value is not None and (type(value) is not int or value < 0):
            return _failure("invalid_response")
        sequence_values[field] = value

    return {
        "ok": True,
        "agent": {
            "name": observed_identity["name"],
            "agent_kind": observed_identity["agent"],
            "workspace_id": observed_identity["workspace_id"],
            "tab_id": observed_identity["tab_id"],
            "pane_id": observed_identity["pane_id"],
            "agent_session_id": session_value,
            "agent_status": agent_status,
            "interactive_ready": interactive_ready,
            "state_change_seq": sequence_values["state_change_seq"],
            "completion_seq": sequence_values["completion_seq"],
        },
    }
