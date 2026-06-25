"""Tool handlers for the multitool plugin.

Each handler receives (args: dict, **kwargs) where kwargs include
task_id and session_id from the registry dispatch chain.
All handlers return a JSON string.
"""

import json
import logging
from typing import Any, Dict

from plugins.multitool.registry import registry
from plugins.multitool.utils import format_error, format_success, postprocess_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# env_connect
# ---------------------------------------------------------------------------

def handle_env_connect(args: Dict[str, Any], **kwargs: Any) -> str:
    """Create a named connection to an SSH or Docker environment."""

    env_type = args.get("type")
    if not env_type:
        return format_error("Missing required parameter: type")

    if env_type not in ("ssh", "docker"):
        return format_error(
            f"Unknown environment type: '{env_type}'. Supported: docker, ssh"
        )

    # Slug handling
    slug = args.get("slug")
    if not slug:
        slug = registry.generate_slug()

    if registry.has(slug):
        return format_error(
            f"Environment '{slug}' already connected. "
            "Use env_disconnect first or choose a different slug."
        )

    cwd = args.get("cwd")
    timeout = args.get("timeout", 180)

    try:
        if env_type == "ssh":
            host = args.get("host")
            user = args.get("user")
            if not host or not user:
                return format_error(
                    "SSH connection requires 'host' and 'user' parameters"
                )

            from tools.environments.ssh import SSHEnvironment

            env = SSHEnvironment(
                host=host,
                user=user,
                cwd=cwd or "~",
                timeout=timeout,
                port=args.get("port", 22),
                key_path=args.get("key_path", ""),
            )

        elif env_type == "docker":
            image = args.get("image")
            if not image:
                return format_error("Docker connection requires 'image' parameter")

            from tools.environments.docker import DockerEnvironment

            env = DockerEnvironment(
                image=image,
                cwd=cwd or "/root",
                timeout=timeout,
                cpu=0,
                memory=0,
                disk=0,
                persistent_filesystem=True,
                task_id=slug,
                volumes=args.get("volumes"),
                auto_mount_cwd=args.get("auto_mount_cwd", False),
                persist_across_processes=False,
            )

        else:
            return format_error(
                f"Unknown environment type: '{env_type}'. Supported: docker, ssh"
            )

        # Capture login-shell snapshot
        env.init_session()

        # Create file-operations wrapper
        from tools.file_operations import ShellFileOperations

        file_ops = ShellFileOperations(env)

        # Register
        registry.connect(slug, env_type, env, file_ops)

        return format_success({
            "slug": slug,
            "type": env_type,
            "cwd": getattr(env, "cwd", cwd or ""),
        })

    except Exception as exc:
        logger.exception("env_connect failed for slug '%s': %s", slug, exc)
        return format_error(f"Connection failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# env_list
# ---------------------------------------------------------------------------

def handle_env_list(args: Dict[str, Any], **kwargs: Any) -> str:
    """List all active environment connections."""
    envs = registry.list_envs()
    return json.dumps({"environments": envs}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# env_tool
# ---------------------------------------------------------------------------

def handle_env_tool(args: Dict[str, Any], **kwargs: Any) -> str:
    """Execute a tool operation on a named environment."""

    env_slug = args.get("env_slug")
    tool_name = args.get("tool_name")
    tool_args = args.get("args", {})
    task_id = kwargs.get("task_id")

    if not env_slug:
        return format_error("Missing required parameter: env_slug")
    if not tool_name:
        return format_error("Missing required parameter: tool_name")

    entry = registry.get(env_slug)
    if entry is None:
        return format_error(
            f"Environment '{env_slug}' not found. "
            "Use env_list to see available environments."
        )

    env, file_ops, _meta = entry

    from plugins.multitool.dispatch import DISPATCH_TABLE

    dispatch_fn = DISPATCH_TABLE.get(tool_name)
    if dispatch_fn is None:
        supported = ", ".join(sorted(DISPATCH_TABLE.keys()))
        return format_error(
            f"Unknown tool_name: '{tool_name}'. Supported: {supported}"
        )

    try:
        return dispatch_fn(env, file_ops, tool_args, task_id)
    except Exception as exc:
        logger.exception("env_tool dispatch failed: %s", exc)
        return format_error(
            f"Tool execution failed: {type(exc).__name__}: {exc}"
        )


# ---------------------------------------------------------------------------
# env_disconnect
# ---------------------------------------------------------------------------

def handle_env_disconnect(args: Dict[str, Any], **kwargs: Any) -> str:
    """Disconnect from a named environment and release its resources."""

    slug = args.get("slug")
    if not slug:
        return format_error("Missing required parameter: slug")

    meta = registry.disconnect(slug)
    if meta is None:
        return format_error(f"Environment '{slug}' not found")

    env = meta.pop("env", None)
    if env is not None:
        try:
            env.cleanup()
        except Exception as exc:
            logger.warning("cleanup for '%s' failed: %s", slug, exc)

    return format_success({"slug": slug, "status": "disconnected"})
