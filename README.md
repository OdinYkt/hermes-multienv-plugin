# hermes-multienv-plugin

Multi-environment tool plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Lets the agent work with multiple SSH and Docker environments simultaneously — connect to N remote servers and containers, run commands, read/write files, execute code on each, all in one turn.

## Features

- **env_connect** — Create named connection to SSH server or Docker container
- **env_list** — List all active environment connections
- **env_tool** — Execute tool operations (terminal, read_file, write_file, patch, search_files, execute_code) on any connected environment
- **env_disconnect** — Close connection and release resources
- **env_file_transfer** — Transfer single files between host and environment (scp/docker exec, secret-safe)

### Supported environments

| Type | Mode | Description |
|---|---|---|
| `ssh` | Key-based auth | Connect to remote server via SSH (ControlMaster connection reuse) |
| `docker` (new) | `image` param | Create and manage a new Docker container |
| `docker` (existing) | `container` param | Attach to an already-running container (no lifecycle management) |

### execute_code

Scripts running on remote environments can call Hermes tools via `hermes_tools` RPC module:

```python
env_tool("serverA", "execute_code", {"code": "
    import hermes_tools
    result = hermes_tools.terminal('ls -la')
    files = hermes_tools.read_file('/etc/hosts')
    hermes_tools.write_file('/tmp/out.txt', files['content'])
    print('done')
"})
```

7 tools available inside sandbox: `terminal`, `read_file`, `write_file`, `patch`, `search_files`, `web_search`, `web_extract`.

## Installation

### Option 1: pip install (recommended)

```bash
pip install git+https://github.com/OdinYkt/hermes-multienv-plugin.git
```

### Option 2: manual

```bash
git clone https://github.com/OdinYkt/hermes-multienv-plugin.git ~/.hermes/plugins/multienv
```

## Usage

The plugin registers 5 tools under the `multienv` toolset. Enable via `hermes tools` or `config.yaml`:

```yaml
tools:
  cli:
    enabled:
      - multienv
```

### Examples

**Connect to SSH server:**
```
env_connect(slug="serverA", type="ssh", host="a.example.com", user="deploy", key_path="~/.ssh/id_rsa")
```

**Run command on serverA:**
```
env_tool(env_slug="serverA", tool_name="terminal", args={"command": "ls -la"})
```

**Read file on serverA:**
```
env_tool(env_slug="serverA", tool_name="read_file", args={"path": "/etc/hosts"})
```

**Connect to existing Docker container:**
```
env_connect(slug="myapp", type="docker", container="running-app-container")
```

**Disconnect:**
```
env_disconnect(slug="serverA")
```

**Transfer a file to an environment (secret-safe):**
```
env_file_transfer(env_slug="serverA", local_path="~/.env", remote_path="/app/.env", direction="upload")
```

**Download a file from an environment:**
```
env_file_transfer(env_slug="serverA", local_path="/tmp/hosts", remote_path="/etc/hosts", direction="download")
```

## Architecture

- **Plugin-only** — no core Hermes files modified
- **EnvironmentRegistry** — thread-safe registry of named environment instances
- **env_tool** — meta-dispatcher: routes `(env_slug, tool_name, args)` to the target environment
- **execute_code Path C** — file-based RPC: script calls `hermes_tools.terminal()` → writes request file on remote → plugin poll loop reads, dispatches to `env.execute()`, writes response file → script continues
- **env_file_transfer** — scp (SSH) or docker exec with stdin/stdout pipe (Docker). File content travels through OS subprocess pipes — never materializes as a Python string, so it cannot leak into `env.execute()` calls or logger output. Safe for secrets.
- **Core isolation** — plugin never touches `_active_environments`, uses its own registry

## Requirements

- Hermes Agent (provides `tools.environments.*`, `tools.file_operations`, `tools.code_execution_tool`)
- Python 3.11+
- Docker (optional — for Docker environment type)
- SSH client (optional — for SSH environment type)

## License

MIT
