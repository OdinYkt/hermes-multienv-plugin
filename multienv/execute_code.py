"""Path A: plain Python execution on a remote environment.

Writes the user's script to a temp directory on the target env via
base64-encoded echo, runs it with python3, and returns stdout.

No Hermes tool-RPC parity — the script cannot call terminal(), read_file(),
etc.  For full RPC parity see Path C (future work).
"""

import base64
import json
import logging
import shlex
import uuid
from typing import Any

from .utils import postprocess_output

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300


def execute_plain_python(env: Any, code: str) -> str:
    """Run *code* as a Python script on *env* and return the result as JSON.

    Args:
        env: A BaseEnvironment instance with an ``execute()`` method.
        code: Python source code to execute.

    Returns:
        JSON string with keys: status, output, tool_calls_made, duration_seconds,
        and optionally error.
    """
    if not code:
        return json.dumps(
            {"status": "error", "error": "Missing required parameter: code",
             "tool_calls_made": 0, "duration_seconds": 0},
            ensure_ascii=False,
        )

    sandbox_id = uuid.uuid4().hex[:12]

    # Resolve a writable temp dir on the target env
    get_temp_dir = getattr(env, "get_temp_dir", None)
    if callable(get_temp_dir):
        try:
            temp_dir = get_temp_dir().rstrip("/") or "/tmp"
        except Exception:
            temp_dir = "/tmp"
    else:
        temp_dir = "/tmp"

    sandbox_dir = f"{temp_dir}/hermes_exec_{sandbox_id}"
    quoted_sandbox_dir = shlex.quote(sandbox_dir)

    try:
        # 1. Verify python3 is available
        py_check = env.execute(
            "command -v python3 >/dev/null 2>&1 && echo OK",
            cwd="/",
            timeout=15,
        )
        if "OK" not in py_check.get("output", ""):
            return json.dumps(
                {"status": "error",
                 "error": "Python 3 is not available in the terminal environment. "
                          "Install Python to use execute_code.",
                 "tool_calls_made": 0, "duration_seconds": 0},
                ensure_ascii=False,
            )

        # 2. Create sandbox directory
        env.execute(
            f"mkdir -p {quoted_sandbox_dir}",
            cwd="/",
            timeout=10,
        )

        # 3. Ship script via base64 (shell-safe, no ARG_MAX issue)
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        script_path = shlex.quote(f"{sandbox_dir}/script.py")
        env.execute(
            f"echo '{encoded}' | base64 -d > {script_path}",
            cwd="/",
            timeout=30,
        )

        # 4. Run the script
        result = env.execute(
            f"cd {quoted_sandbox_dir} && PYTHONDONTWRITEBYTECODE=1 python3 script.py",
            timeout=_DEFAULT_TIMEOUT,
        )

        stdout = result.get("output", "")
        exit_code = result.get("returncode", -1)

        # 5. Post-process output
        stdout = postprocess_output(stdout)

        status = "success" if exit_code == 0 else "error"
        response: dict[str, Any] = {
            "status": status,
            "output": stdout,
            "tool_calls_made": 0,
            "duration_seconds": 0,
        }
        if exit_code == 124:
            response["status"] = "timeout"
            response["error"] = f"Script timed out after {_DEFAULT_TIMEOUT}s and was killed."
        elif exit_code == 130:
            response["status"] = "interrupted"
        elif exit_code != 0:
            response["error"] = f"Script exited with code {exit_code}"

        return json.dumps(response, ensure_ascii=False)

    except Exception as exc:
        logger.exception("execute_plain_python failed: %s", exc)
        return json.dumps(
            {"status": "error",
             "error": f"Execution failed: {type(exc).__name__}: {exc}",
             "tool_calls_made": 0, "duration_seconds": 0},
            ensure_ascii=False,
        )

    finally:
        # 6. Cleanup sandbox dir
        try:
            env.execute(
                f"rm -rf {quoted_sandbox_dir}",
                cwd="/",
                timeout=15,
            )
        except Exception:
            logger.debug("Failed to clean up sandbox %s", sandbox_dir)


# ---------------------------------------------------------------------------
# Path C: full tool-RPC parity
# ---------------------------------------------------------------------------

import threading
import time
from typing import Any, Optional


def _plugin_dispatch(env, file_ops, tool_name, tool_args, task_id):
    """Dispatch a tool call to the plugin env — NOT core handle_function_call."""
    if tool_name == "terminal":
        command = tool_args.get("command", "")
        if not command:
            return json.dumps({"error": "Missing parameter: command"})
        cwd = tool_args.get("workdir") or tool_args.get("cwd")
        timeout = tool_args.get("timeout")
        try:
            result = env.execute(command, cwd=cwd, timeout=timeout)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({
            "output": result.get("output", ""),
            "exit_code": result.get("returncode", -1),
        })

    elif tool_name == "read_file":
        path = tool_args.get("path", "")
        if not path:
            return json.dumps({"error": "Missing parameter: path"})
        try:
            r = file_ops.read_file(path, offset=tool_args.get("offset", 1), limit=tool_args.get("limit", 500))
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        if r.error:
            return json.dumps({"error": r.error})
        return json.dumps({"content": r.content, "total_lines": r.total_lines})

    elif tool_name == "write_file":
        path = tool_args.get("path", "")
        content = tool_args.get("content", "")
        if not path:
            return json.dumps({"error": "Missing parameter: path"})
        try:
            r = file_ops.write_file(path, content)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        if r.error:
            return json.dumps({"error": r.error})
        return json.dumps({"status": "ok", "bytes_written": r.bytes_written})

    elif tool_name == "search_files":
        pattern = tool_args.get("pattern", "")
        if not pattern:
            return json.dumps({"error": "Missing parameter: pattern"})
        try:
            r = file_ops.search(
                pattern,
                path=tool_args.get("path", "."),
                target=tool_args.get("target", "content"),
                file_glob=tool_args.get("file_glob"),
                limit=tool_args.get("limit", 50),
                offset=tool_args.get("offset", 0),
                output_mode=tool_args.get("output_mode", "content"),
                context=tool_args.get("context", 0),
            )
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        if r.error:
            return json.dumps({"error": r.error})
        matches = []
        for m in r.matches:
            if hasattr(m, "__dict__"):
                matches.append(vars(m))
            else:
                matches.append(str(m))
        return json.dumps({"matches": matches, "files": r.files, "total_count": r.total_count})

    elif tool_name == "patch":
        mode = tool_args.get("mode", "replace")
        try:
            if mode == "replace":
                r = file_ops.patch_replace(
                    tool_args.get("path", ""),
                    tool_args.get("old_string", ""),
                    tool_args.get("new_string", ""),
                    replace_all=tool_args.get("replace_all", False),
                )
            else:
                r = file_ops.patch_v4a(tool_args.get("patch", ""))
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        if r.error:
            return json.dumps({"error": r.error})
        return json.dumps({"status": "ok", "diff": r.diff, "files_modified": r.files_modified})

    elif tool_name in ("web_search", "web_extract"):
        # NOT env-coupled — safe to use core dispatcher
        try:
            from model_tools import handle_function_call
            return handle_function_call(tool_name, tool_args, task_id=task_id)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def _plugin_rpc_poll_loop(
    env, file_ops, rpc_dir, task_id,
    tool_call_log, tool_call_counter,
    max_tool_calls, allowed_tools, stop_event,
):
    """Poll the remote filesystem for tool-call requests and dispatch them."""
    try:
        from tools.code_execution_tool import _TERMINAL_BLOCKED_PARAMS
    except ImportError:
        _TERMINAL_BLOCKED_PARAMS = set()

    poll_interval = 0.1
    quoted_rpc_dir = shlex.quote(rpc_dir)

    while not stop_event.is_set():
        try:
            ls_result = env.execute(
                f"ls -1 {quoted_rpc_dir}/req_* 2>/dev/null || true",
                cwd="/", timeout=10,
            )
            output = ls_result.get("output", "").strip()
            if not output:
                stop_event.wait(poll_interval)
                continue

            req_files = sorted([
                f.strip() for f in output.split("\n")
                if f.strip() and not f.strip().endswith(".tmp")
            ])

            for req_file in req_files:
                if stop_event.is_set():
                    break

                # Read request
                read_result = env.execute(
                    f"cat {shlex.quote(req_file)}", cwd="/", timeout=10,
                )
                try:
                    request = json.loads(read_result.get("output", ""))
                except (json.JSONDecodeError, ValueError):
                    env.execute(f"rm -f {shlex.quote(req_file)}", cwd="/", timeout=5)
                    continue

                tool_name = request.get("tool", "")
                tool_args = request.get("args", {})
                seq = request.get("seq", 0)
                seq_str = f"{seq:06d}"
                res_file = f"{rpc_dir}/res_{seq_str}"

                # Enforce allow-list
                if tool_name not in allowed_tools:
                    available = ", ".join(sorted(allowed_tools))
                    tool_result = json.dumps({"error": f"Tool '{tool_name}' not available. Available: {available}"})
                elif tool_call_counter[0] >= max_tool_calls:
                    tool_result = json.dumps({"error": f"Tool call limit reached ({max_tool_calls})"})
                else:
                    # Strip forbidden terminal params
                    if tool_name == "terminal" and isinstance(tool_args, dict):
                        for p in _TERMINAL_BLOCKED_PARAMS:
                            tool_args.pop(p, None)

                    tool_result = _plugin_dispatch(env, file_ops, tool_name, tool_args, task_id)
                    tool_call_counter[0] += 1

                # Write response atomically
                encoded = base64.b64encode(tool_result.encode("utf-8")).decode("ascii")
                env.execute(
                    f"echo '{encoded}' | base64 -d > {shlex.quote(res_file)}.tmp"
                    f" && mv {shlex.quote(res_file)}.tmp {shlex.quote(res_file)}",
                    cwd="/", timeout=60,
                )

                # Remove request file
                env.execute(f"rm -f {shlex.quote(req_file)}", cwd="/", timeout=5)

        except Exception:
            if not stop_event.is_set():
                stop_event.wait(poll_interval)


def execute_with_rpc(env, file_ops, code, enabled_tools=None, task_id=None):
    """Run *code* on *env* with full tool-RPC parity.

    Ships hermes_tools.py + script.py to the env, starts an RPC poll loop
    that dispatches tool calls to the same env, and returns stdout.
    """
    if not code:
        return json.dumps(
            {"status": "error", "error": "Missing required parameter: code",
             "tool_calls_made": 0, "duration_seconds": 0},
            ensure_ascii=False,
        )

    # Core imports (all public functions)
    try:
        from tools.code_execution_tool import (
            generate_hermes_tools_module,
            _ship_file_to_remote,
            _env_temp_dir,
            SANDBOX_ALLOWED_TOOLS,
        )
    except ImportError as exc:
        return json.dumps(
            {"status": "error", "error": f"Core import failed: {exc}",
             "tool_calls_made": 0, "duration_seconds": 0},
            ensure_ascii=False,
        )

    # Determine sandbox tools
    sandbox_tools = frozenset(SANDBOX_ALLOWED_TOOLS)
    if enabled_tools:
        sandbox_tools = sandbox_tools & set(enabled_tools)

    sandbox_id = uuid.uuid4().hex[:12]
    temp_dir = _env_temp_dir(env)
    sandbox_dir = f"{temp_dir}/hermes_exec_{sandbox_id}"
    rpc_dir = f"{sandbox_dir}/rpc"
    quoted_sandbox_dir = shlex.quote(sandbox_dir)

    tool_call_log = []
    tool_call_counter = [0]
    max_tool_calls = 50
    stop_event = threading.Event()
    rpc_thread = None
    exec_start = time.monotonic()

    try:
        # 1. Verify python3
        py_check = env.execute(
            "command -v python3 >/dev/null 2>&1 && echo OK",
            cwd="/", timeout=15,
        )
        if "OK" not in py_check.get("output", ""):
            return json.dumps(
                {"status": "error",
                 "error": "Python 3 is not available in the terminal environment.",
                 "tool_calls_made": 0, "duration_seconds": 0},
                ensure_ascii=False,
            )

        # 2. Create sandbox + rpc dir
        env.execute(f"mkdir -p {shlex.quote(rpc_dir)}", cwd="/", timeout=10)

        # 3. Generate and ship hermes_tools.py + script.py
        tools_src = generate_hermes_tools_module(list(sandbox_tools), transport="file")
        _ship_file_to_remote(env, f"{sandbox_dir}/hermes_tools.py", tools_src)
        _ship_file_to_remote(env, f"{sandbox_dir}/script.py", code)

        # 4. Start RPC poll loop
        rpc_thread = threading.Thread(
            target=_plugin_rpc_poll_loop,
            args=(env, file_ops, rpc_dir, task_id,
                  tool_call_log, tool_call_counter,
                  max_tool_calls, sandbox_tools, stop_event),
            daemon=True,
        )
        rpc_thread.start()

        # 5. Run the script
        env_prefix = (
            f"HERMES_RPC_DIR={shlex.quote(rpc_dir)} "
            f"PYTHONDONTWRITEBYTECODE=1"
        )
        result = env.execute(
            f"cd {quoted_sandbox_dir} && {env_prefix} python3 script.py",
            timeout=_DEFAULT_TIMEOUT,
        )

        stdout = result.get("output", "")
        exit_code = result.get("returncode", -1)
        stdout = postprocess_output(stdout)

        status = "success" if exit_code == 0 else "error"
        duration = round(time.monotonic() - exec_start, 2)
        response = {
            "status": status,
            "output": stdout,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }
        if exit_code == 124:
            response["status"] = "timeout"
            response["error"] = f"Script timed out after {_DEFAULT_TIMEOUT}s and was killed."
        elif exit_code == 130:
            response["status"] = "interrupted"
        elif exit_code != 0:
            response["error"] = f"Script exited with code {exit_code}"

        return json.dumps(response, ensure_ascii=False)

    except Exception as exc:
        logger.exception("execute_with_rpc failed: %s", exc)
        duration = round(time.monotonic() - exec_start, 2)
        return json.dumps(
            {"status": "error",
             "error": f"Execution failed: {type(exc).__name__}: {exc}",
             "tool_calls_made": tool_call_counter[0], "duration_seconds": duration},
            ensure_ascii=False,
        )

    finally:
        stop_event.set()
        if rpc_thread is not None:
            rpc_thread.join(timeout=5)
        try:
            env.execute(f"rm -rf {quoted_sandbox_dir}", cwd="/", timeout=15)
        except Exception:
            logger.debug("Failed to clean up sandbox %s", sandbox_dir)
