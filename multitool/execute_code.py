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

from plugins.multitool.utils import postprocess_output

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
