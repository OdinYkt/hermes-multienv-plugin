"""Dispatch table mapping tool_name → handler function for env_tool.

Each dispatch function receives (env, file_ops, args, task_id) and returns
a JSON string.
"""

import json
import logging
from typing import Any, Dict, Optional

from multitool.utils import format_error, format_success, postprocess_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# terminal
# ---------------------------------------------------------------------------

def dispatch_terminal(env, file_ops, args: Dict[str, Any], task_id: Optional[str]) -> str:
    command = args.get("command")
    if not command:
        return format_error("Missing required parameter: command")

    cwd = args.get("workdir") or args.get("cwd")
    timeout = args.get("timeout")

    try:
        result = env.execute(command, cwd=cwd, timeout=timeout)
    except Exception as exc:
        return format_error(f"Command execution failed: {type(exc).__name__}: {exc}")

    output = postprocess_output(result.get("output", ""))
    exit_code = result.get("returncode", -1)

    return json.dumps(
        {"output": output, "exit_code": exit_code},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

def dispatch_read_file(env, file_ops, args: Dict[str, Any], task_id: Optional[str]) -> str:
    path = args.get("path")
    if not path:
        return format_error("Missing required parameter: path")

    try:
        result = file_ops.read_file(
            path,
            offset=args.get("offset", 1),
            limit=args.get("limit", 500),
        )
    except Exception as exc:
        return format_error(f"read_file failed: {type(exc).__name__}: {exc}")

    if result.error:
        return format_error(result.error)

    return json.dumps(
        {"content": result.content, "total_lines": result.total_lines},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

def dispatch_write_file(env, file_ops, args: Dict[str, Any], task_id: Optional[str]) -> str:
    path = args.get("path")
    content = args.get("content")
    if not path or content is None:
        return format_error("Missing required parameters: path and content")

    try:
        result = file_ops.write_file(path, content)
    except Exception as exc:
        return format_error(f"write_file failed: {type(exc).__name__}: {exc}")

    if result.error:
        return format_error(result.error)

    return format_success({"bytes_written": result.bytes_written})


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------

def dispatch_patch(env, file_ops, args: Dict[str, Any], task_id: Optional[str]) -> str:
    mode = args.get("mode", "replace")

    try:
        if mode == "replace":
            path = args.get("path")
            old_string = args.get("old_string")
            new_string = args.get("new_string")
            if not path or old_string is None or new_string is None:
                return format_error(
                    "Missing required parameters: path, old_string, new_string"
                )
            result = file_ops.patch_replace(
                path,
                old_string,
                new_string,
                replace_all=args.get("replace_all", False),
            )
        else:
            patch_content = args.get("patch")
            if not patch_content:
                return format_error("Missing required parameter: patch")
            result = file_ops.patch_v4a(patch_content)

    except Exception as exc:
        return format_error(f"patch failed: {type(exc).__name__}: {exc}")

    if result.error:
        return format_error(result.error)

    return format_success({
        "diff": result.diff,
        "files_modified": result.files_modified,
    })


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------

def dispatch_search_files(env, file_ops, args: Dict[str, Any], task_id: Optional[str]) -> str:
    pattern = args.get("pattern")
    if not pattern:
        return format_error("Missing required parameter: pattern")

    try:
        result = file_ops.search(
            pattern,
            path=args.get("path", "."),
            target=args.get("target", "content"),
            file_glob=args.get("file_glob"),
            limit=args.get("limit", 50),
            offset=args.get("offset", 0),
            output_mode=args.get("output_mode", "content"),
            context=args.get("context", 0),
        )
    except Exception as exc:
        return format_error(f"search_files failed: {type(exc).__name__}: {exc}")

    if result.error:
        return format_error(result.error)

    # Convert SearchMatch objects to dicts safely
    matches = []
    for m in result.matches:
        if hasattr(m, "__dict__"):
            matches.append(vars(m))
        elif hasattr(m, "_asdict"):
            matches.append(m._asdict())
        else:
            matches.append(str(m))

    return json.dumps(
        {"matches": matches, "files": result.files, "total_count": result.total_count},
        ensure_ascii=False,
    )


def dispatch_execute_code(env, file_ops, args: Dict[str, Any], task_id: Optional[str]) -> str:
    from multitool.execute_code import execute_with_rpc

    code = args.get("code", "")
    return execute_with_rpc(env, file_ops, code, task_id=task_id)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

DISPATCH_TABLE = {
    "terminal": dispatch_terminal,
    "read_file": dispatch_read_file,
    "write_file": dispatch_write_file,
    "patch": dispatch_patch,
    "search_files": dispatch_search_files,
    "execute_code": dispatch_execute_code,
}
