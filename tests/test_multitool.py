"""Tests for the multitool plugin — 3 checks covering the real user path.

Check 1: Plugin discovery + register (no external deps)
Check 2: Docker E2E (skip if no Docker)
Check 3: Error paths (no external deps)
"""

import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure both project root (for multitool) and hermes-agent (for tools, hermes_cli, etc.) are on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
for _p in [_PROJECT_ROOT, _PROJECT_ROOT / "hermes-agent"]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to a temp dir so tests don't touch ~/.hermes/."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)
    # Clear the registry singleton between tests
    from multitool.registry import registry
    registry.clear()
    yield
    registry.clear()


# ---------------------------------------------------------------------------
# Check 1 — Plugin discovery + register
# ---------------------------------------------------------------------------

def test_plugin_discovered_and_registered():
    """Hermes finds multitool plugin, register(ctx) called, 4 tools in registry."""
    from multitool import register
    from multitool.schemas import (
        ENV_CONNECT_SCHEMA,
        ENV_DISCONNECT_SCHEMA,
        ENV_LIST_SCHEMA,
        ENV_TOOL_SCHEMA,
    )
    from tools.registry import registry as tool_registry

    # Simulate what PluginManager does — call register() with a fake ctx
    class FakeCtx:
        def __init__(self):
            self.registered_tools = []
            self.hooks = []

        def register_tool(self, **kwargs):
            self.registered_tools.append(kwargs)
            tool_registry.register(**kwargs)

        def register_hook(self, name, handler):
            self.hooks.append((name, handler))

    ctx = FakeCtx()
    register(ctx)

    # 4 tools registered
    assert len(ctx.registered_tools) == 4
    tool_names = {t["name"] for t in ctx.registered_tools}
    assert tool_names == {"env_connect", "env_list", "env_tool", "env_disconnect"}

    # All under "multitool" toolset
    for t in ctx.registered_tools:
        assert t["toolset"] == "multitool"

    # on_session_end hook registered
    assert any(name == "on_session_end" for name, _ in ctx.hooks)

    # Schemas are valid dicts with required keys
    for schema in [ENV_CONNECT_SCHEMA, ENV_LIST_SCHEMA, ENV_TOOL_SCHEMA, ENV_DISCONNECT_SCHEMA]:
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        assert schema["parameters"].get("type") == "object"


# ---------------------------------------------------------------------------
# Check 2 — Docker E2E: connect → terminal → read_file → write_file → disconnect
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("docker"), reason="Docker not available")
def test_docker_e2e_user_path():
    """Full user path: connect Docker → run command → read file → write file → disconnect."""
    from multitool.handlers import (
        handle_env_connect,
        handle_env_disconnect,
        handle_env_list,
        handle_env_tool,
    )

    # 1. Connect to python:3.12-slim container (has bash + python3)
    result = json.loads(handle_env_connect({
        "slug": "testbox",
        "type": "docker",
        "image": "python:3.12-slim",
        "cwd": "/root",
    }, task_id="test"))
    assert result.get("status") == "ok", f"connect failed: {result}"
    assert result["slug"] == "testbox"
    assert result["type"] == "docker"

    try:
        # 2. Run terminal command
        result = json.loads(handle_env_tool({
            "env_slug": "testbox",
            "tool_name": "terminal",
            "args": {"command": "echo hello > /tmp/test.txt && echo OK"},
        }, task_id="test"))
        assert result.get("exit_code") == 0, f"terminal failed: {result}"
        assert "OK" in result.get("output", "")

        # 3. Read file
        result = json.loads(handle_env_tool({
            "env_slug": "testbox",
            "tool_name": "read_file",
            "args": {"path": "/tmp/test.txt"},
        }, task_id="test"))
        assert "hello" in result.get("content", ""), f"read_file failed: {result}"

        # 4. Write file
        result = json.loads(handle_env_tool({
            "env_slug": "testbox",
            "tool_name": "write_file",
            "args": {"path": "/tmp/written.txt", "content": "plugin works"},
        }, task_id="test"))
        assert result.get("status") == "ok", f"write_file failed: {result}"

        # 5. Verify written file via terminal
        result = json.loads(handle_env_tool({
            "env_slug": "testbox",
            "tool_name": "terminal",
            "args": {"command": "cat /tmp/written.txt"},
        }, task_id="test"))
        assert "plugin works" in result.get("output", ""), f"verify failed: {result}"

        # 6. execute_code (Path A — plain Python)
        result = json.loads(handle_env_tool({
            "env_slug": "testbox",
            "tool_name": "execute_code",
            "args": {"code": "print(2 + 2)"},
        }, task_id="test"))
        assert result.get("status") == "success", f"execute_code failed: {result}"
        assert "4" in result.get("output", "")

    finally:
        # 7. Disconnect (always, even if assertions fail)
        result = json.loads(handle_env_disconnect({
            "slug": "testbox",
        }, task_id="test"))
        assert result.get("status") == "disconnected", f"disconnect failed: {result}"
        assert result["slug"] == "testbox"

    # 8. List should be empty after disconnect
    result = json.loads(handle_env_list({}, task_id="test"))
    assert result["environments"] == [], f"list not empty: {result}"


# ---------------------------------------------------------------------------
# Check 3 — Error paths
# ---------------------------------------------------------------------------

def test_error_paths():
    """Invalid slug, missing params, unknown tool_name → correct errors."""

    from multitool.handlers import handle_env_connect, handle_env_tool

    # 3a. env_tool with invalid slug
    result = json.loads(handle_env_tool({
        "env_slug": "nonexistent",
        "tool_name": "terminal",
        "args": {"command": "ls"},
    }, task_id="test"))
    assert "error" in result, f"expected error, got: {result}"
    assert "not found" in result["error"].lower()

    # 3b. env_connect SSH without host
    result = json.loads(handle_env_connect({
        "type": "ssh",
        "user": "deploy",
    }, task_id="test"))
    assert "error" in result, f"expected error, got: {result}"
    assert "host" in result["error"].lower() or "required" in result["error"].lower()

    # 3c. env_connect with unknown type
    result = json.loads(handle_env_connect({
        "type": "kubernetes",
    }, task_id="test"))
    assert "error" in result, f"expected error, got: {result}"
    assert "docker" in result["error"].lower() or "ssh" in result["error"].lower()

    # 3d. env_connect missing type entirely
    result = json.loads(handle_env_connect({
        "host": "example.com",
    }, task_id="test"))
    assert "error" in result, f"expected error, got: {result}"
    assert "type" in result["error"].lower()

    # 3e. env_tool with missing env_slug
    result = json.loads(handle_env_tool({
        "tool_name": "terminal",
        "args": {"command": "ls"},
    }, task_id="test"))
    assert "error" in result
    assert "env_slug" in result["error"].lower()

    # 3f. env_tool with missing tool_name
    result = json.loads(handle_env_tool({
        "env_slug": "whatever",
        "args": {},
    }, task_id="test"))
    assert "error" in result
    assert "tool_name" in result["error"].lower()

    # 3g. env_disconnect with missing slug
    from multitool.handlers import handle_env_disconnect
    result = json.loads(handle_env_disconnect({}, task_id="test"))
    assert "error" in result
    assert "slug" in result["error"].lower()


# ---------------------------------------------------------------------------
# Check 4 — Existing container E2E: connect to running container, verify it survives disconnect
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("docker"), reason="Docker not available")
def test_existing_container_e2e():
    """Connect to an existing running container, run a command, disconnect, verify container still running."""
    import subprocess
    from multitool.handlers import handle_env_connect, handle_env_tool, handle_env_disconnect, handle_env_list

    container_name = "multitool-existing-test"

    # 0. Start a container manually (simulates an externally-managed container)
    try:
        subprocess.run(["docker", "rm", "-f", container_name],
                       capture_output=True, timeout=10)
        result = subprocess.run(
            ["docker", "run", "-d", "--name", container_name, "python:3.12-slim", "sleep", "300"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"docker run failed: {result.stderr}"
    except Exception as exc:
        pytest.skip(f"Could not start container: {exc}")

    try:
        # 1. Connect to the EXISTING container
        result = json.loads(handle_env_connect({
            "slug": "existingbox",
            "type": "docker",
            "container": container_name,
            "cwd": "/root",
        }, task_id="test"))
        assert result.get("status") == "ok", f"connect failed: {result}"
        assert result["slug"] == "existingbox"

        # 2. Run terminal command
        result = json.loads(handle_env_tool({
            "env_slug": "existingbox",
            "tool_name": "terminal",
            "args": {"command": "echo EXISTING_OK"},
        }, task_id="test"))
        assert result.get("exit_code") == 0, f"terminal failed: {result}"
        assert "EXISTING_OK" in result.get("output", "")

        # 3. Disconnect (should NOT stop or remove the container)
        result = json.loads(handle_env_disconnect({
            "slug": "existingbox",
        }, task_id="test"))
        assert result.get("status") == "disconnected"

        # 4. Verify container is STILL running (not stopped by disconnect)
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=10,
        )
        assert result.stdout.strip() == "true", (
            f"Container '{container_name}' was stopped by disconnect! "
            f"inspect output: {result.stdout.strip()}"
        )

        # 5. List should be empty after disconnect
        result = json.loads(handle_env_list({}, task_id="test"))
        assert result["environments"] == []

    finally:
        # Cleanup: stop and remove the container we started
        subprocess.run(["docker", "rm", "-f", container_name],
                       capture_output=True, timeout=15)

    # 6. Error: connect to nonexistent container
    result = json.loads(handle_env_connect({
        "slug": "badbox",
        "type": "docker",
        "container": "nonexistent-container-xyz",
    }, task_id="test"))
    assert "error" in result
    assert "not found" in result["error"].lower() or "not running" in result["error"].lower()


# ---------------------------------------------------------------------------
# Check 5 — execute_code Path C: tool-RPC parity on Docker
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("docker"), reason="Docker not available")
def test_execute_code_path_c_tool_rpc():
    """Script on remote can call hermes_tools.terminal/read_file/write_file via RPC."""
    from multitool.handlers import handle_env_connect, handle_env_tool, handle_env_disconnect

    # 1. Connect to Docker
    result = json.loads(handle_env_connect({
        "slug": "rpctest",
        "type": "docker",
        "image": "python:3.12-slim",
        "cwd": "/root",
    }, task_id="test"))
    assert result.get("status") == "ok", f"connect failed: {result}"

    try:
        # 2. Script calls hermes_tools.terminal() — tool-RPC round-trip
        code = """
import hermes_tools
result = hermes_tools.terminal('echo RPC_WORKS')
print('TOOL_RESULT:', result)
"""
        result = json.loads(handle_env_tool({
            "env_slug": "rpctest",
            "tool_name": "execute_code",
            "args": {"code": code},
        }, task_id="test"))
        assert result.get("status") == "success", f"execute_code failed: {result}"
        assert "RPC_WORKS" in result.get("output", ""), f"RPC output missing: {result}"
        assert result.get("tool_calls_made", 0) >= 1, f"expected >=1 tool call, got: {result}"

        # 3. Script calls hermes_tools.write_file() + read_file()
        code2 = """
import hermes_tools
hermes_tools.write_file('/tmp/rpc_test.txt', 'RPC_FILE_CONTENT')
r = hermes_tools.read_file('/tmp/rpc_test.txt')
print('FILE_CONTENT:', r)
"""
        result = json.loads(handle_env_tool({
            "env_slug": "rpctest",
            "tool_name": "execute_code",
            "args": {"code": code2},
        }, task_id="test"))
        assert result.get("status") == "success", f"execute_code write+read failed: {result}"
        assert "RPC_FILE_CONTENT" in result.get("output", ""), f"file content missing: {result}"
        assert result.get("tool_calls_made", 0) >= 2, f"expected >=2 tool calls, got: {result}"

    finally:
        handle_env_disconnect({"slug": "rpctest"}, task_id="test")
