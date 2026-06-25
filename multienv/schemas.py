"""JSON Schema definitions for multienv plugin tools."""

from typing import Any, Dict

# ---------------------------------------------------------------------------
# env_connect — create a named connection to an SSH or Docker environment
# ---------------------------------------------------------------------------

ENV_CONNECT_SCHEMA: Dict[str, Any] = {
    "name": "env_connect",
    "description": (
        "Connect to a remote execution environment (SSH server or Docker container). "
        "Returns a slug that identifies this environment for use with env_tool. "
        "Use env_list to see active connections and env_disconnect to close them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "User-defined name for this environment (e.g. 'serverA', 'db'). "
                    "If omitted, an auto-generated slug like 'env-1' is assigned. "
                    "Must be unique — connecting with an existing slug returns an error."
                ),
            },
            "type": {
                "type": "string",
                "enum": ["ssh", "docker"],
                "description": "Type of execution environment to connect to.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Initial working directory inside the environment. "
                    "SSH default: '~'. Docker default: '/root'."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Command timeout in seconds (default: 180).",
                "default": 180,
                "minimum": 1,
            },
            # SSH-specific
            "host": {
                "type": "string",
                "description": "SSH: remote server hostname or IP address.",
            },
            "user": {
                "type": "string",
                "description": "SSH: username for SSH login.",
            },
            "port": {
                "type": "integer",
                "description": "SSH: port number (default: 22).",
                "default": 22,
                "minimum": 1,
                "maximum": 65535,
            },
            "key_path": {
                "type": "string",
                "description": "SSH: path to the private key file for authentication.",
            },
            # Docker-specific
            "image": {
                "type": "string",
                "description": (
                    "Docker: image name to create a NEW container from (e.g. 'node:22', 'python:3.12'). "
                    "Use 'container' instead to attach to an existing running container."
                ),
            },
            "container": {
                "type": "string",
                "description": (
                    "Docker: name or ID of an EXISTING running container to attach to. "
                    "When provided, 'image' is ignored. The container must already be running. "
                    "The plugin will NOT stop or remove this container on disconnect."
                ),
            },
            "volumes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Docker: volume mounts as 'host:container' strings. "
                    "Example: ['/host/dir:/container/dir']."
                ),
            },
            "auto_mount_cwd": {
                "type": "boolean",
                "description": (
                    "Docker: mount the launch working directory into /workspace "
                    "inside the container (default: false)."
                ),
                "default": False,
            },
        },
        "required": ["type"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# env_list — list all active environment connections
# ---------------------------------------------------------------------------

ENV_LIST_SCHEMA: Dict[str, Any] = {
    "name": "env_list",
    "description": (
        "List all active environment connections created by env_connect. "
        "Returns each environment's slug, type, status, cwd, and connected_at timestamp."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# env_tool — execute a tool operation on a named environment
# ---------------------------------------------------------------------------

ENV_TOOL_SCHEMA: Dict[str, Any] = {
    "name": "env_tool",
    "description": (
        "Execute a tool operation (terminal, read_file, write_file, patch, "
        "search_files, execute_code) on a named environment connected via env_connect. "
        "The 'args' object should contain the same parameters as the corresponding "
        "core tool's schema (visible in the system prompt). "
        "Example: env_tool(env_slug='serverA', tool_name='terminal', "
        "args={'command': 'ls -la'})."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "env_slug": {
                "type": "string",
                "description": (
                    "Identifier of the target environment (returned by env_connect "
                    "or shown by env_list)."
                ),
            },
            "tool_name": {
                "type": "string",
                "enum": [
                    "terminal",
                    "read_file",
                    "write_file",
                    "patch",
                    "search_files",
                    "execute_code",
                ],
                "description": "Name of the operation to execute on the target environment.",
            },
            "args": {
                "type": "object",
                "description": (
                    "Parameters for the named tool — same structure as the core tool's "
                    "own schema. For terminal: {command, workdir, timeout}. "
                    "For read_file: {path, offset, limit}. "
                    "For write_file: {path, content}. "
                    "For patch: {path, old_string, new_string, replace_all, mode, patch}. "
                    "For search_files: {pattern, path, target, file_glob, limit, output_mode, context}. "
                    "For execute_code: {code}."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["env_slug", "tool_name", "args"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# env_disconnect — close a named connection
# ---------------------------------------------------------------------------

ENV_DISCONNECT_SCHEMA: Dict[str, Any] = {
    "name": "env_disconnect",
    "description": (
        "Disconnect from a named environment and release its resources. "
        "For SSH: syncs .hermes/ changes back to host and closes the SSH connection. "
        "For Docker: stops and removes the container (unless persistent). "
        "After disconnect, the slug is no longer valid — use env_list to verify."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Identifier of the environment to disconnect (from env_connect or env_list).",
            },
        },
        "required": ["slug"],
        "additionalProperties": False,
    },
}
