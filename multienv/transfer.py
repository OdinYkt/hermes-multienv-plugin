"""File transfer handler — scp/docker cp based, secret-safe.

Transfers a single file between the host and a connected environment.
Uses scp (SSH) or docker cp (Docker) so file content travels through
OS subprocess pipes — it never materialises as a Python string and
therefore cannot leak into env.execute() calls or logger output.
"""
import json
import logging
import os
import shlex
import subprocess
import time
from typing import Any, Dict, Optional

from .registry import registry
from .utils import format_error, format_success

logger = logging.getLogger(__name__)

_LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50 MB


def handle_env_file_transfer(args: Dict[str, Any], **kwargs: Any) -> str:
    """Transfer a single file between host and environment.

    Returns JSON string with status, direction, local_path, remote_path,
    bytes, and optionally warning for large files.
    """
    env_slug = args.get("env_slug")
    local_path = args.get("local_path")
    remote_path = args.get("remote_path")
    direction = args.get("direction", "upload")

    # --- parameter validation ------------------------------------------------
    if not env_slug:
        return format_error("Missing required parameter: env_slug")
    if not local_path:
        return format_error("Missing required parameter: local_path")
    if not remote_path:
        return format_error("Missing required parameter: remote_path")
    if direction not in ("upload", "download"):
        return format_error(
            f"Invalid direction: '{direction}'. Supported: upload, download"
        )

    # --- resolve environment -------------------------------------------------
    entry = registry.get(env_slug)
    if entry is None:
        return format_error(
            f"Environment '{env_slug}' not found. "
            "Use env_list to see available environments."
        )

    env, _file_ops, meta = entry
    env_type = meta.get("type", "")

    # --- upload-specific validation ------------------------------------------
    if direction == "upload":
        if not os.path.exists(local_path):
            return format_error(f"Local file not found: {local_path}")
        if os.path.isdir(local_path):
            return format_error(
                f"Local path is a directory, not a file: {local_path}. "
                "Only single files are supported."
            )

    # --- large file warning --------------------------------------------------
    file_size = 0
    if direction == "upload" and os.path.isfile(local_path):
        file_size = os.path.getsize(local_path)

    response_extra: Dict[str, Any] = {}
    if file_size > _LARGE_FILE_THRESHOLD:
        logger.warning(
            "env_file_transfer: large file %s (%d bytes, >50MB limit)",
            local_path, file_size,
        )
        response_extra["warning"] = "File exceeds 50MB soft limit"

    # --- dispatch to backend -------------------------------------------------
    start = time.monotonic()

    try:
        if env_type == "ssh":
            transferred = _ssh_transfer(env, local_path, remote_path, direction)
        elif env_type == "docker":
            transferred = _docker_transfer(env, local_path, remote_path, direction)
        else:
            return format_error(
                f"File transfer not supported for environment type: '{env_type}'"
            )
    except Exception as exc:
        logger.error(
            "env_file_transfer failed: slug=%s direction=%s local=%s remote=%s error=%s",
            env_slug, direction, local_path, remote_path, exc,
        )
        return format_error(f"Transfer failed: {type(exc).__name__}: {exc}")

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "env_file_transfer: slug=%s direction=%s local=%s remote=%s bytes=%d duration_ms=%d",
        env_slug, direction, local_path, remote_path, transferred, duration_ms,
    )

    result: Dict[str, Any] = {
        "direction": direction,
        "local_path": local_path,
        "remote_path": remote_path,
        "bytes": transferred,
    }
    result.update(response_extra)
    return format_success(result)


# ---------------------------------------------------------------------------
# SSH backend — scp via ControlMaster
# ---------------------------------------------------------------------------

def _ssh_transfer(env: Any, local_path: str, remote_path: str, direction: str) -> int:
    """Transfer via scp using the env's ControlMaster socket.

    Returns bytes transferred.
    """
    control_socket = getattr(env, "control_socket", None)
    port = getattr(env, "port", 22)
    key_path = getattr(env, "key_path", "")
    host = getattr(env, "host", "")
    user = getattr(env, "user", "")

    if not host or not user:
        raise RuntimeError("SSH environment missing host or user attributes")

    if direction == "upload":
        # Create parent directory on remote via ssh
        remote_dir = os.path.dirname(remote_path)
        if remote_dir:
            ssh_cmd = ["ssh", "-o", f"ControlPath={control_socket}"]
            if port != 22:
                ssh_cmd.extend(["-p", str(port)])
            if key_path:
                ssh_cmd.extend(["-i", key_path])
            ssh_cmd.append(f"{user}@{host}")
            ssh_cmd.append(f"mkdir -p {shlex.quote(remote_dir)}")
            subprocess.run(
                ssh_cmd, capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
            )

        # scp -p preserves file permissions
        scp_cmd = ["scp", "-p", "-o", f"ControlPath={control_socket}"]
        if port != 22:
            scp_cmd.extend(["-P", str(port)])
        if key_path:
            scp_cmd.extend(["-i", key_path])
        scp_cmd.append(local_path)
        scp_cmd.append(f"{user}@{host}:{remote_path}")

        result = subprocess.run(
            scp_cmd, capture_output=True, timeout=300, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"scp failed: {stderr}")

        return os.path.getsize(local_path)

    else:  # download
        # Create parent directory on local host
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)

        scp_cmd = ["scp", "-p", "-o", f"ControlPath={control_socket}"]
        if port != 22:
            scp_cmd.extend(["-P", str(port)])
        if key_path:
            scp_cmd.extend(["-i", key_path])
        scp_cmd.append(f"{user}@{host}:{remote_path}")
        scp_cmd.append(local_path)

        result = subprocess.run(
            scp_cmd, capture_output=True, timeout=300, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"scp failed: {stderr}")

        return os.path.getsize(local_path) if os.path.exists(local_path) else 0


# ---------------------------------------------------------------------------
# Docker backend — docker exec with stdin/stdout pipe
# ---------------------------------------------------------------------------
# Uses `docker exec -i ... cat > remote` (upload) and `docker exec ... cat remote` (download)
# instead of `docker cp` because docker cp silently fails on Docker Desktop for Linux
# when the container has bind mounts. Content travels through OS pipe — never
# materialises as a Python string, so it remains secret-safe.

def _docker_transfer(env: Any, local_path: str, remote_path: str, direction: str) -> int:
    """Transfer via docker exec with stdin/stdout pipe.

    Works with both DockerEnvironment (_container_id) and
    ExistingDockerEnvironment (_container).

    Returns bytes transferred.
    """
    docker_exe = getattr(env, "_docker_exe", None) or "docker"
    container_id = (
        getattr(env, "_container_id", None)
        or getattr(env, "_container", None)
    )

    if not container_id:
        raise RuntimeError("Cannot determine container ID for Docker environment")

    if direction == "upload":
        # Create parent directory inside container
        remote_dir = os.path.dirname(remote_path)
        if remote_dir:
            subprocess.run(
                [docker_exe, "exec", container_id, "mkdir", "-p", remote_dir],
                capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
            )

        # Pipe file content through docker exec stdin — content stays in OS pipe
        with open(local_path, "rb") as f:
            result = subprocess.run(
                [docker_exe, "exec", "-i", container_id,
                 "sh", "-c", f"cat > {shlex.quote(remote_path)}"],
                stdin=f, capture_output=True, timeout=300,
            )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"docker exec upload failed: {stderr}")

        # Preserve permissions
        local_mode = os.stat(local_path).st_mode
        mode_str = oct(local_mode & 0o777)[2:]  # e.g. '755'
        subprocess.run(
            [docker_exe, "exec", container_id, "chmod", mode_str, remote_path],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        )

        return os.path.getsize(local_path)

    else:  # download
        # Create parent directory on local host
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)

        # Pipe file content from docker exec stdout — content stays in OS pipe
        with open(local_path, "wb") as f:
            result = subprocess.run(
                [docker_exe, "exec", container_id, "cat", remote_path],
                stdout=f, stderr=subprocess.PIPE, timeout=300, stdin=subprocess.DEVNULL,
            )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"docker exec download failed: {stderr}")

        return os.path.getsize(local_path) if os.path.exists(local_path) else 0
