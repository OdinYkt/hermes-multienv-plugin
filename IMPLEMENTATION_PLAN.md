# Implementation Plan — Multitool Plugin

Subagent-driven development plan. Each task = atomic, delegable unit.

---

## File Structure

```
plugins/multitool/
├── plugin.yaml              # Manifest
├── __init__.py              # register(ctx) entry point, tool registration, hooks
├── registry.py              # EnvironmentRegistry class
├── handlers.py              # Tool handler functions (env_connect, env_list, env_tool, env_disconnect)
├── schemas.py               # JSON Schema dicts for 4 tools
├── dispatch.py              # env_tool dispatch table (tool_name → handler)
├── execute_code.py          # Path A: plain Python execution on remote env
└── utils.py                 # Slug generation, result formatting, error helpers

tests/plugins/
├── test_multitool_registry.py     # EnvironmentRegistry unit tests
├── test_multitool_connect.py      # env_connect handler tests
├── test_multitool_tool.py         # env_tool dispatch tests
├── test_multitool_disconnect.py   # env_disconnect handler tests
└── test_multitool_integration.py  # End-to-end: connect → tool → disconnect
```

---

## Task Breakdown

### Wave 1 — Foundation (parallel, no deps)

---

#### T1: plugin.yaml + schemas.py

**Files:** `plugins/multitool/plugin.yaml`, `plugins/multitool/schemas.py`
**Category:** quick
**Skills:** []
**Complexity:** S
**Deps:** none

**Create:**

`plugin.yaml`:
```yaml
name: multitool
version: 0.1.0
description: "Multi-environment tool plugin — work with multiple SSH and Docker environments simultaneously"
author: NousResearch
kind: standalone
provides_tools:
  - env_connect
  - env_list
  - env_tool
  - env_disconnect
hooks:
  - on_session_end
```

`schemas.py` — 4 JSON Schema dicts:
- `ENV_CONNECT_SCHEMA`: properties = slug (string, optional), type (enum: ssh, docker), cwd (string), timeout (int), host (string), user (string), port (int), key_path (string), image (string), volumes (array), auto_mount_cwd (bool). Required: type. additionalProperties: False.
- `ENV_LIST_SCHEMA`: no properties. Required: none.
- `ENV_TOOL_SCHEMA`: properties = env_slug (string), tool_name (string, enum: terminal, read_file, write_file, patch, search_files, execute_code), args (object, additionalProperties: True). Required: env_slug, tool_name, args.
- `ENV_DISCONNECT_SCHEMA`: properties = slug (string). Required: slug.

**QA:** `python -c "from plugins.multitool.schemas import *; print(ENV_CONNECT_SCHEMA['name'])"` → prints `env_connect`

---

#### T2: registry.py — EnvironmentRegistry

**Files:** `plugins/multitool/registry.py`
**Category:** deep
**Skills:** []
**Complexity:** M
**Deps:** none

**Create:**

`EnvironmentRegistry` class:
```python
class EnvironmentRegistry:
    """Thread-safe registry of named execution environments."""
    
    def __init__(self):
        self._envs: dict[str, BaseEnvironment] = {}
        self._file_ops: dict[str, ShellFileOperations] = {}
        self._meta: dict[str, dict] = {}  # {type, status, cwd, connected_at}
        self._lock = threading.Lock()
        self._counter = 0  # for auto-slug generation
    
    def connect(self, slug, env_type, env, file_ops, meta) -> str
    def get(self, slug) -> tuple[BaseEnvironment, ShellFileOperations, dict] | None
    def list_envs(self) -> list[dict]
    def disconnect(self, slug) -> dict | None  # returns meta for cleanup
    def has(self, slug) -> bool
    def generate_slug(self) -> str  # "env-1", "env-2", ...
    def cleanup_all(self) -> None  # for on_session_end
```

Thread-safe with `threading.Lock`. `disconnect` returns meta so caller can call `env.cleanup()`. `cleanup_all` iterates all envs, calls `cleanup()` on each.

**QA:** Unit test — connect 3 envs (mock BaseEnvironment), list returns 3, disconnect 1 returns 2, disconnect nonexistent → None.

---

#### T3: utils.py — helpers

**Files:** `plugins/multitool/utils.py`
**Category:** quick
**Skills:** []
**Complexity:** S
**Deps:** none

**Create:**

- `format_success(data: dict) -> str` — `json.dumps({"status": "ok", **data})`
- `format_error(msg: str, **extra) -> str` — `json.dumps({"error": msg, **extra})`
- `truncate_output(output: str, max_bytes: int = 50000) -> str` — head + tail + omitted count (same as core)
- `strip_ansi_and_redact(output: str) -> str` — import `strip_ansi` from `tools.ansi_strip`, `redact_sensitive_text` from `agent.redact`

**QA:** `format_error("test")` → `{"error": "test"}`. `truncate_output("x"*100, 50)` → truncated.

---

### Wave 2 — Core handlers (parallel, depends on Wave 1)

---

#### T4: handlers.py — env_connect handler

**Files:** `plugins/multitool/handlers.py` (partial — env_connect only)
**Category:** deep
**Skills:** []
**Complexity:** M
**Deps:** T1 (schemas), T2 (registry), T3 (utils)

**Create `handle_env_connect(args, **kwargs) -> str`:**

1. Extract params from args: slug, type, cwd, timeout, host, user, port, key_path, image, volumes, auto_mount_cwd
2. Validate required: type must be "ssh" or "docker". SSH requires host + user. Docker requires image.
3. If slug omitted → `registry.generate_slug()`
4. If slug already in registry → return error "already connected"
5. Create env based on type:
   - SSH: `from tools.environments.ssh import SSHEnvironment; env = SSHEnvironment(host, user, cwd=cwd or "~", timeout=timeout or 180, port=port or 22, key_path=key_path or "")`
   - Docker: `from tools.environments.docker import DockerEnvironment; env = DockerEnvironment(image=image, cwd=cwd or "/root", timeout=timeout or 180, ...)` (use sensible defaults for cpu/memory/disk)
6. `env.init_session()`
7. Create `ShellFileOperations(env)`
8. `registry.connect(slug, type, env, file_ops, meta)`
9. Return `{"slug": slug, "status": "connected", "type": type, "cwd": env.cwd}`
10. On any exception → return `{"error": str(e)}`, don't leave partial state

**Edge cases (from BDD):**
- Duplicate slug → error, existing env untouched
- SSH missing host/user → error before creating env
- SSH connection failure → error, no registry entry
- Docker not installed → error
- Unknown type → error "Supported: docker, ssh"

**QA:** Mock SSHEnvironment — verify init_session called, registry has entry. Mock duplicate slug → error. Missing host → error.

---

#### T5: handlers.py — env_list handler

**Files:** `plugins/multitool/handlers.py` (add env_list)
**Category:** quick
**Skills:** []
**Complexity:** S
**Deps:** T2 (registry), T3 (utils)

**Create `handle_env_list(args, **kwargs) -> str`:**

1. `envs = registry.list_envs()`
2. Return `{"environments": envs}`

**QA:** Empty registry → `{"environments": []}`. 2 envs → 2 entries.

---

#### T6: dispatch.py — env_tool dispatch table

**Files:** `plugins/multitool/dispatch.py`
**Category:** deep
**Skills:** []
**Complexity:** M
**Deps:** T2 (registry), T3 (utils)

**Create dispatch table mapping tool_name → function(env, file_ops, args, task_id) -> str:**

```python
def dispatch_terminal(env, file_ops, args, task_id):
    command = args.get("command")
    if not command:
        return format_error("Missing required parameter: command")
    cwd = args.get("workdir") or args.get("cwd")
    timeout = args.get("timeout")
    result = env.execute(command, cwd=cwd, timeout=timeout)
    output = truncate_output(result.get("output", ""))
    output = strip_ansi_and_redact(output)
    return json.dumps({"output": output, "exit_code": result.get("returncode", -1)})

def dispatch_read_file(env, file_ops, args, task_id):
    path = args.get("path")
    if not path:
        return format_error("Missing required parameter: path")
    result = file_ops.read_file(path, offset=args.get("offset", 1), limit=args.get("limit", 500))
    if result.error:
        return format_error(result.error)
    return json.dumps({"content": result.content, "total_lines": result.total_lines})

def dispatch_write_file(env, file_ops, args, task_id):
    path = args.get("path")
    content = args.get("content")
    if not path or content is None:
        return format_error("Missing required parameters: path and content")
    result = file_ops.write_file(path, content)
    if result.error:
        return format_error(result.error)
    return json.dumps({"status": "ok", "bytes_written": result.bytes_written})

def dispatch_patch(env, file_ops, args, task_id):
    mode = args.get("mode", "replace")
    if mode == "replace":
        path = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not path or old_string is None or new_string is None:
            return format_error("Missing required parameters: path, old_string, new_string")
        result = file_ops.patch_replace(path, old_string, new_string, replace_all=args.get("replace_all", False))
    else:
        patch_content = args.get("patch")
        if not patch_content:
            return format_error("Missing required parameter: patch")
        result = file_ops.patch_v4a(patch_content)
    if result.error:
        return format_error(result.error)
    return json.dumps({"status": "ok", "diff": result.diff, "files_modified": result.files_modified})

def dispatch_search_files(env, file_ops, args, task_id):
    pattern = args.get("pattern")
    if not pattern:
        return format_error("Missing required parameter: pattern")
    result = file_ops.search(
        pattern, path=args.get("path", "."),
        target=args.get("target", "content"),
        file_glob=args.get("file_glob"),
        limit=args.get("limit", 50),
        offset=args.get("offset", 0),
        output_mode=args.get("output_mode", "content"),
        context=args.get("context", 0),
    )
    if result.error:
        return format_error(result.error)
    return json.dumps({"matches": [m.__dict__ if hasattr(m, '__dict__') else str(m) for m in result.matches], "files": result.files, "total_count": result.total_count})

def dispatch_execute_code(env, file_ops, args, task_id):
    # MVP: Path A — plain Python
    from plugins.multitool.execute_code import execute_plain_python
    return execute_plain_python(env, args.get("code", ""))

DISPATCH_TABLE = {
    "terminal": dispatch_terminal,
    "read_file": dispatch_read_file,
    "write_file": dispatch_write_file,
    "patch": dispatch_patch,
    "search_files": dispatch_search_files,
    "execute_code": dispatch_execute_code,
}
```

**QA:** Mock env + file_ops — each dispatch function returns correct JSON for valid args, error for missing args.

---

#### T7: execute_code.py — Path A (plain Python)

**Files:** `plugins/multitool/execute_code.py`
**Category:** quick
**Skills:** []
**Complexity:** S
**Deps:** T3 (utils)

**Create `execute_plain_python(env, code) -> str`:**

```python
import base64, json, shlex, uuid
from plugins.multitool.utils import truncate_output, strip_ansi_and_redact

def execute_plain_python(env, code: str) -> str:
    if not code:
        return json.dumps({"status": "error", "error": "Missing required parameter: code"})
    
    sandbox_id = uuid.uuid4().hex[:12]
    temp_dir = "/tmp"  # or use env.get_temp_dir()
    sandbox_dir = f"{temp_dir}/hermes_exec_{sandbox_id}"
    
    try:
        # Check python3 available
        py_check = env.execute("command -v python3 >/dev/null 2>&1 && echo OK", cwd="/", timeout=15)
        if "OK" not in py_check.get("output", ""):
            return json.dumps({"status": "error", "error": "Python 3 is not available in the terminal environment."})
        
        # Create sandbox dir
        env.execute(f"mkdir -p {shlex.quote(sandbox_dir)}", cwd="/", timeout=10)
        
        # Ship script via base64
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        env.execute(f"echo '{encoded}' | base64 -d > {shlex.quote(sandbox_dir + '/script.py')}", cwd="/", timeout=30)
        
        # Run script
        result = env.execute(f"cd {shlex.quote(sandbox_dir)} && python3 script.py", timeout=300)
        stdout = result.get("output", "")
        exit_code = result.get("returncode", -1)
        
        # Post-process
        stdout = truncate_output(stdout)
        stdout = strip_ansi_and_redact(stdout)
        
        status = "success" if exit_code == 0 else "error"
        return json.dumps({
            "status": status,
            "output": stdout,
            "tool_calls_made": 0,
            "duration_seconds": 0,  # could measure
            **({"error": f"Script exited with code {exit_code}"} if exit_code != 0 else {}),
        }, ensure_ascii=False)
    
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc), "tool_calls_made": 0})
    
    finally:
        try:
            env.execute(f"rm -rf {shlex.quote(sandbox_dir)}", cwd="/", timeout=15)
        except Exception:
            pass
```

**QA:** Mock env — verify mkdir, ship, execute, cleanup called. No python3 → error. Exit code 0 → success. Exit code 1 → error.

---

### Wave 3 — Integration (parallel, depends on Wave 2)

---

#### T8: handlers.py — env_tool handler + env_disconnect handler

**Files:** `plugins/multitool/handlers.py` (add env_tool + env_disconnect)
**Category:** deep
**Skills:** []
**Complexity:** M
**Deps:** T2 (registry), T6 (dispatch), T7 (execute_code)

**Create `handle_env_tool(args, **kwargs) -> str`:**

```python
def handle_env_tool(args, **kwargs):
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
        return format_error(f"Environment '{env_slug}' not found. Use env_list to see available environments.")
    
    env, file_ops, meta = entry
    
    from plugins.multitool.dispatch import DISPATCH_TABLE
    dispatch_fn = DISPATCH_TABLE.get(tool_name)
    if dispatch_fn is None:
        return format_error(f"Unknown tool_name: '{tool_name}'. Supported: {', '.join(DISPATCH_TABLE.keys())}")
    
    try:
        return dispatch_fn(env, file_ops, tool_args, task_id)
    except Exception as exc:
        return format_error(f"Tool execution failed: {type(exc).__name__}: {exc}")
```

**Create `handle_env_disconnect(args, **kwargs) -> str`:**

```python
def handle_env_disconnect(args, **kwargs):
    slug = args.get("slug")
    if not slug:
        return format_error("Missing required parameter: slug")
    
    entry = registry.disconnect(slug)  # returns meta dict or None
    if entry is None:
        return format_error(f"Environment '{slug}' not found")
    
    # entry contains the env instance for cleanup
    env = entry.get("env")
    if env:
        try:
            env.cleanup()
        except Exception:
            pass
    
    return json.dumps({"slug": slug, "status": "disconnected"})
```

**QA:** Mock registry — env_tool with valid slug → dispatch called. Invalid slug → error. Unknown tool_name → error. env_disconnect valid → cleanup called. Invalid → error.

---

#### T9: __init__.py — register() entry point + on_session_end hook

**Files:** `plugins/multitool/__init__.py`
**Category:** quick
**Skills:** []
**Complexity:** S
**Deps:** T1 (schemas), T4/T5/T8 (handlers)

**Create:**

```python
from plugins.multitool.schemas import (
    ENV_CONNECT_SCHEMA, ENV_LIST_SCHEMA,
    ENV_TOOL_SCHEMA, ENV_DISCONNECT_SCHEMA,
)
from plugins.multitool.registry import registry as _registry
from plugins.multitool.handlers import (
    handle_env_connect, handle_env_list,
    handle_env_tool, handle_env_disconnect,
)

_TOOLS = [
    ("env_connect", ENV_CONNECT_SCHEMA, handle_env_connect, "🔗"),
    ("env_list", ENV_LIST_SCHEMA, handle_env_list, "📋"),
    ("env_tool", ENV_TOOL_SCHEMA, handle_env_tool, "🔧"),
    ("env_disconnect", ENV_DISCONNECT_SCHEMA, handle_env_disconnect, "🔌"),
]

def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="multitool",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )
    ctx.register_hook("on_session_end", _on_session_end)

def _on_session_end(**kwargs):
    _registry.cleanup_all()
```

**QA:** `python -c "from plugins.multitool import register; print(callable(register))"` → True

---

### Wave 4 — Tests (single task, depends on Wave 3)

---

#### T10: test_multitool.py — 3 checks covering real user path

**Files:** `tests/plugins/test_multitool.py`
**Category:** deep
**Skills:** ["test-driven-development"]
**Complexity:** M
**Deps:** T9 (full plugin)

**3 checks:**

**Check 1 — Plugin discovery + register**
```python
def test_plugin_discovered_and_registered():
    """Hermes finds multitool plugin, register(ctx) called, 4 tools in registry."""
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    # simulate plugin discovery
    mgr = PluginManager()
    mgr.discover()
    # find multitool
    assert any(p.manifest.name == "multitool" for p in mgr._plugins)
    # register it
    from plugins.multitool import register
    class FakeCtx:
        def register_tool(self, **kw): registry.register(**kw)
        def register_hook(self, *a, **kw): pass
    register(FakeCtx())
    # 4 tools under multitool toolset
    tool_names = registry.get_tool_names_for_toolset("multitool")
    assert set(tool_names) == {"env_connect", "env_list", "env_tool", "env_disconnect"}
```

**Check 2 — Docker E2E: connect → terminal → read_file → write_file → disconnect** (skip if no Docker)
```python
@pytest.mark.skipif(not shutil.which("docker"), reason="no docker")
def test_docker_e2e_user_path():
    """Full user path: connect Docker → run command → read file → write file → disconnect."""
    from plugins.multitool.handlers import handle_env_connect, handle_env_tool, handle_env_disconnect, handle_env_list
    # 1. Connect to alpine container
    result = json.loads(handle_env_connect({
        "slug": "testbox",
        "type": "docker",
        "image": "alpine:3.19",
        "cwd": "/root",
    }, task_id="test"))
    assert result["status"] == "connected"
    assert result["slug"] == "testbox"
    # 2. Run terminal command
    result = json.loads(handle_env_tool({
        "env_slug": "testbox",
        "tool_name": "terminal",
        "args": {"command": "echo hello > /tmp/test.txt && echo OK"},
    }, task_id="test"))
    assert result["exit_code"] == 0
    assert "OK" in result["output"]
    # 3. Read file
    result = json.loads(handle_env_tool({
        "env_slug": "testbox",
        "tool_name": "read_file",
        "args": {"path": "/tmp/test.txt"},
    }, task_id="test"))
    assert "hello" in result["content"]
    # 4. Write file
    result = json.loads(handle_env_tool({
        "env_slug": "testbox",
        "tool_name": "write_file",
        "args": {"path": "/tmp/written.txt", "content": "plugin works"},
    }, task_id="test"))
    assert result["status"] == "ok"
    # 5. Verify written file
    result = json.loads(handle_env_tool({
        "env_slug": "testbox",
        "tool_name": "terminal",
        "args": {"command": "cat /tmp/written.txt"},
    }, task_id="test"))
    assert "plugin works" in result["output"]
    # 6. Disconnect
    result = json.loads(handle_env_disconnect({"slug": "testbox"}, task_id="test"))
    assert result["status"] == "disconnected"
    # 7. List empty
    result = json.loads(handle_env_list({}, task_id="test"))
    assert result["environments"] == []
```

**Check 3 — Error paths**
```python
def test_error_paths():
    """Invalid slug, missing params, unknown tool_name → correct errors."""
    from plugins.multitool.handlers import handle_env_tool, handle_env_connect
    # Invalid slug
    result = json.loads(handle_env_tool({
        "env_slug": "nonexistent",
        "tool_name": "terminal",
        "args": {"command": "ls"},
    }, task_id="test"))
    assert "error" in result
    assert "not found" in result["error"].lower()
    # Missing required params (SSH without host)
    result = json.loads(handle_env_connect({
        "type": "ssh",
        "user": "deploy",
    }, task_id="test"))
    assert "error" in result
    assert "host" in result["error"].lower() or "required" in result["error"].lower()
    # Unknown tool_name
    # First connect a mock env so we get past slug check
    result = json.loads(handle_env_connect({
        "slug": "errbox",
        "type": "docker",
        "image": "alpine:3.19",
    }, task_id="test"))
    if result.get("status") == "connected":
        result = json.loads(handle_env_tool({
            "env_slug": "errbox",
            "tool_name": "nonexistent_tool",
            "args": {},
        }, task_id="test"))
        assert "error" in result
        assert "unknown" in result["error"].lower() or "supported" in result["error"].lower()
        # cleanup
        handle_env_disconnect({"slug": "errbox"}, task_id="test")
```

**QA:** `scripts/run_tests.sh tests/plugins/test_multitool.py -v` — 3 tests, Check 2 skipped if no Docker.

---

## Parallel Execution Waves

```
Wave 1 (parallel):
  T1: plugin.yaml + schemas.py
  T2: registry.py
  T3: utils.py

Wave 2 (parallel, after Wave 1):
  T4: handlers.py — env_connect     [deps: T1, T2, T3]
  T5: handlers.py — env_list        [deps: T2, T3]
  T6: dispatch.py                   [deps: T2, T3]
  T7: execute_code.py               [deps: T3]

Wave 3 (parallel, after Wave 2):
  T8: handlers.py — env_tool + env_disconnect  [deps: T2, T6, T7]
  T9: __init__.py — register() + hooks          [deps: T1, T4, T5, T8]

Wave 4 (after Wave 3):
  T10: test_multitool.py — 3 checks             [deps: T9]
```

---

## Stage Gates (between waves)

| Gate | After | Verify |
|---|---|---|
| Gate 1 | Wave 1 | `python -c "from plugins.multitool.schemas import ENV_CONNECT_SCHEMA; print('ok')"` + `python -c "from plugins.multitool.registry import EnvironmentRegistry; print('ok')"` |
| Gate 2 | Wave 2 | `python -c "from plugins.multitool.handlers import handle_env_connect; print('ok')"` + `python -c "from plugins.multitool.dispatch import DISPATCH_TABLE; print(len(DISPATCH_TABLE))"` → 6 |
| Gate 3 | Wave 3 | `python -c "from plugins.multitool import register; print(callable(register))"` → True |

---

## Risk Areas

| Risk | Mitigation |
|---|---|
| DockerEnvironment constructor has many required params | Use `_create_environment("docker", image, cwd, timeout, ...)` from terminal_tool instead of direct constructor — handles defaults |
| ShellFileOperations result types have complex fields | Use `dataclasses.asdict()` or manual field extraction in dispatch |
| Plugin import path — `plugins.multitool.xxx` | Verify Hermes plugin discovery adds plugin dir to sys.path. Check how google_meet imports its modules. |
| Thread safety of EnvironmentRegistry | Lock around all dict operations. Subagents may run concurrent tool calls. |
| SSHEnvironment constructor blocks (connects + syncs) | Wrap in try/except, return error JSON on timeout |
| env.execute() may raise on disconnected env | Catch in dispatch, return error JSON |
| Docker not available in test env | Check 2 skipped with `@pytest.mark.skipif` |

---

## Test Plan

### Objective
Verify the real user path: plugin discovery → connect → operate → disconnect. Plus error paths.

### Prerequisites
- Python 3.11+
- Hermes repo at `hermes-agent/`
- pytest + unittest.mock
- Docker (for Check 2 — skip if unavailable)

### Test Cases
1. **Plugin discovery**: Hermes finds multitool, register(ctx) called, 4 tools registered under "multitool" toolset
2. **Docker E2E**: connect(docker, alpine) → terminal(echo > file) → read_file → write_file → verify → disconnect → list empty
3. **Error paths**: invalid slug → error, SSH missing host → error, unknown tool_name → error

### Success Criteria
- Check 1 + Check 3 pass always (no external deps)
- Check 2 passes when Docker available, skips otherwise
- No core file modifications
- Plugin discovered by Hermes plugin discovery

### How to Execute
```bash
cd hermes-agent
scripts/run_tests.sh tests/plugins/test_multitool.py -v
```
