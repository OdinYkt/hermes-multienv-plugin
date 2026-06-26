"""Tests for env_file_transfer — scp/docker cp based, secret-safe file transfer.

Unit tests (no external deps):
  - Schema validation
  - Error paths
  - Mocked SSH/Docker transfer (verify correct subprocess calls)
  - Secret safety (file content never in subprocess args)
  - Large file warning
  - Directory rejection
  - Plugin registration (5th tool)

E2E tests (skip if no Docker):
  - Docker upload + download round-trip
  - Permissions preserved
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure both project root (for multienv) and hermes-agent (for tools) are on sys.path
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
    from multienv.registry import registry
    registry.clear()
    yield
    registry.clear()


def _make_mock_ssh_env(tmp_path):
    """Create a mock SSHEnvironment with the attributes transfer.py needs."""
    env = MagicMock()
    env.control_socket = str(tmp_path / "test.sock")
    env.host = "test.example.com"
    env.user = "deploy"
    env.port = 22
    env.key_path = ""
    env.cwd = "~"
    return env


def _make_mock_docker_env():
    """Create a mock DockerEnvironment (new container) with _container_id."""
    env = MagicMock()
    env._container_id = "abc123container"
    env._docker_exe = "docker"
    env._container = None
    env.cwd = "/root"
    return env


def _make_mock_existing_docker_env():
    """Create a mock ExistingDockerEnvironment with _container name."""
    env = MagicMock()
    env._container_id = None
    env._docker_exe = "docker"
    env._container = "running-app"
    env.cwd = "/"
    return env


def _register_mock_env(slug, env, env_type):
    """Register a mock env in the plugin registry."""
    from multienv.registry import registry
    file_ops = MagicMock()
    registry.connect(slug, env_type, env, file_ops)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestSchema:
    def test_schema_exists_and_valid(self):
        """ENV_FILE_TRANSFER_SCHEMA is importable and has required keys."""
        from multienv.schemas import ENV_FILE_TRANSFER_SCHEMA
        assert ENV_FILE_TRANSFER_SCHEMA["name"] == "env_file_transfer"
        assert "description" in ENV_FILE_TRANSFER_SCHEMA
        params = ENV_FILE_TRANSFER_SCHEMA["parameters"]
        assert params["type"] == "object"
        props = params["properties"]
        assert "env_slug" in props
        assert "local_path" in props
        assert "remote_path" in props
        assert "direction" in props
        assert set(params["required"]) == {"env_slug", "local_path", "remote_path"}
        assert props["direction"]["default"] == "upload"
        assert set(props["direction"]["enum"]) == {"upload", "download"}
        assert params["additionalProperties"] is False

    def test_schema_description_mentions_secret_safety(self):
        """Schema description should mention that content doesn't pass through logs."""
        from multienv.schemas import ENV_FILE_TRANSFER_SCHEMA
        desc = ENV_FILE_TRANSFER_SCHEMA["description"].lower()
        # Should mention scp/docker cp or the secret-safe nature
        assert "scp" in desc or "docker cp" in desc or "log" in desc or "content" in desc


# ---------------------------------------------------------------------------
# Error path tests (no external deps)
# ---------------------------------------------------------------------------

class TestErrorPaths:
    def test_missing_env_slug(self):
        from multienv.transfer import handle_env_file_transfer
        result = json.loads(handle_env_file_transfer({
            "local_path": "/tmp/x",
            "remote_path": "/tmp/x",
        }, task_id="test"))
        assert "error" in result
        assert "env_slug" in result["error"].lower()

    def test_missing_local_path(self):
        from multienv.transfer import handle_env_file_transfer
        result = json.loads(handle_env_file_transfer({
            "env_slug": "serverA",
            "remote_path": "/tmp/x",
        }, task_id="test"))
        assert "error" in result
        assert "local_path" in result["error"].lower()

    def test_missing_remote_path(self):
        from multienv.transfer import handle_env_file_transfer
        result = json.loads(handle_env_file_transfer({
            "env_slug": "serverA",
            "local_path": "/tmp/x",
        }, task_id="test"))
        assert "error" in result
        assert "remote_path" in result["error"].lower()

    def test_nonexistent_slug(self):
        from multienv.transfer import handle_env_file_transfer
        result = json.loads(handle_env_file_transfer({
            "env_slug": "nonexistent",
            "local_path": "/tmp/x",
            "remote_path": "/tmp/x",
        }, task_id="test"))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_invalid_direction(self):
        from multienv.transfer import handle_env_file_transfer
        _register_mock_env("testbox", _make_mock_docker_env(), "docker")
        result = json.loads(handle_env_file_transfer({
            "env_slug": "testbox",
            "local_path": "/tmp/x",
            "remote_path": "/tmp/x",
            "direction": "sideways",
        }, task_id="test"))
        assert "error" in result
        assert "direction" in result["error"].lower()

    def test_nonexistent_local_file_upload(self, tmp_path):
        from multienv.transfer import handle_env_file_transfer
        _register_mock_env("testbox", _make_mock_docker_env(), "docker")
        result = json.loads(handle_env_file_transfer({
            "env_slug": "testbox",
            "local_path": str(tmp_path / "nonexistent"),
            "remote_path": "/tmp/x",
            "direction": "upload",
        }, task_id="test"))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_directory_as_local_file_upload(self, tmp_path):
        from multienv.transfer import handle_env_file_transfer
        _register_mock_env("testbox", _make_mock_docker_env(), "docker")
        subdir = tmp_path / "somedir"
        subdir.mkdir()
        result = json.loads(handle_env_file_transfer({
            "env_slug": "testbox",
            "local_path": str(subdir),
            "remote_path": "/tmp/dir",
            "direction": "upload",
        }, task_id="test"))
        assert "error" in result
        assert "directory" in result["error"].lower()


# ---------------------------------------------------------------------------
# Mocked SSH transfer tests
# ---------------------------------------------------------------------------

class TestSSHTransfer:
    def test_upload_calls_scp_with_controlmaster(self, tmp_path):
        """Upload via SSH should call scp with -p and ControlPath."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello world")

        env = _make_mock_ssh_env(tmp_path)
        _register_mock_env("serverA", env, "ssh")

        with patch("subprocess.run") as mock_run:
            # First call: mkdir -p, second: scp
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "serverA",
                "local_path": str(local_file),
                "remote_path": "/app/test.txt",
                "direction": "upload",
            }, task_id="test"))

        assert result.get("status") == "ok", f"Expected ok, got: {result}"
        assert result["direction"] == "upload"
        assert result["bytes"] == len("hello world")

        # Verify scp was called (second subprocess.run call)
        calls = mock_run.call_args_list
        assert len(calls) >= 2  # mkdir + scp

        # Find the scp call
        scp_call = calls[-1]
        scp_cmd = scp_call.args[0]
        assert "scp" in scp_cmd[0]
        assert "-p" in scp_cmd  # preserve permissions
        assert "-o" in scp_cmd
        # Verify ControlPath is in the command
        cp_idx = scp_cmd.index("-o") + 1 if "-o" in scp_cmd else -1
        assert cp_idx < len(scp_cmd)
        assert "ControlPath" in scp_cmd[cp_idx]

    def test_download_calls_scp_reversed(self, tmp_path):
        """Download via SSH should call scp with remote first, local second."""
        env = _make_mock_ssh_env(tmp_path)
        _register_mock_env("serverA", env, "ssh")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "serverA",
                "local_path": str(tmp_path / "downloaded.txt"),
                "remote_path": "/etc/hosts",
                "direction": "download",
            }, task_id="test"))

        assert result.get("status") == "ok", f"Expected ok, got: {result}"
        assert result["direction"] == "download"

        # Find scp call — should have remote path before local
        calls = mock_run.call_args_list
        scp_call = calls[-1]
        scp_cmd = scp_call.args[0]
        assert "scp" in scp_cmd[0]
        # Remote path should contain host:remote format
        remote_spec = f"deploy@test.example.com:/etc/hosts"
        assert remote_spec in scp_cmd
        # Local path should be last arg
        assert str(tmp_path / "downloaded.txt") == scp_cmd[-1]

    def test_ssh_with_custom_port(self, tmp_path):
        """SCP should use -P for custom port."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("data")

        env = _make_mock_ssh_env(tmp_path)
        env.port = 2222
        _register_mock_env("serverB", env, "ssh")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            json.loads(handle_env_file_transfer({
                "env_slug": "serverB",
                "local_path": str(local_file),
                "remote_path": "/tmp/test.txt",
                "direction": "upload",
            }, task_id="test"))

        calls = mock_run.call_args_list
        scp_cmd = calls[-1].args[0]
        assert "-P" in scp_cmd
        port_idx = scp_cmd.index("-P") + 1
        assert scp_cmd[port_idx] == "2222"

    def test_ssh_with_key_path(self, tmp_path):
        """SCP should use -i for key_path."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("data")

        env = _make_mock_ssh_env(tmp_path)
        env.key_path = "~/.ssh/custom_key"
        _register_mock_env("serverC", env, "ssh")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            json.loads(handle_env_file_transfer({
                "env_slug": "serverC",
                "local_path": str(local_file),
                "remote_path": "/tmp/test.txt",
                "direction": "upload",
            }, task_id="test"))

        calls = mock_run.call_args_list
        scp_cmd = calls[-1].args[0]
        assert "-i" in scp_cmd
        key_idx = scp_cmd.index("-i") + 1
        assert scp_cmd[key_idx] == "~/.ssh/custom_key"

    def test_ssh_mkdir_parent_before_scp(self, tmp_path):
        """Upload should mkdir -p parent dir on remote before scp."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("data")

        env = _make_mock_ssh_env(tmp_path)
        _register_mock_env("serverA", env, "ssh")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            json.loads(handle_env_file_transfer({
                "env_slug": "serverA",
                "local_path": str(local_file),
                "remote_path": "/new/deep/path/test.txt",
                "direction": "upload",
            }, task_id="test"))

        calls = mock_run.call_args_list
        # First call should be ssh mkdir -p
        mkdir_cmd = calls[0].args[0]
        assert "ssh" in mkdir_cmd[0]
        # The command should contain mkdir -p and the parent dir
        # ssh command: ssh -o ControlPath=... user@host "mkdir -p /new/deep/path"
        full_cmd_str = " ".join(mkdir_cmd)
        assert "mkdir" in full_cmd_str
        assert "/new/deep/path" in full_cmd_str

    def test_ssh_download_creates_local_parent(self, tmp_path):
        """Download should create local parent directory."""
        env = _make_mock_ssh_env(tmp_path)
        _register_mock_env("serverA", env, "ssh")

        deep_local = tmp_path / "new" / "deep" / "downloaded.txt"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            json.loads(handle_env_file_transfer({
                "env_slug": "serverA",
                "local_path": str(deep_local),
                "remote_path": "/tmp/remote.txt",
                "direction": "download",
            }, task_id="test"))

        # Local parent dir should exist
        assert deep_local.parent.exists()


# ---------------------------------------------------------------------------
# Mocked Docker transfer tests
# ---------------------------------------------------------------------------

class TestDockerTransfer:
    def test_upload_calls_docker_exec(self, tmp_path):
        """Upload via Docker should pipe file content through docker exec -i cat > remote."""
        local_file = tmp_path / "config.yaml"
        local_file.write_text("key: value")

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/etc/app/config.yaml",
                "direction": "upload",
            }, task_id="test"))

        assert result.get("status") == "ok", f"Expected ok, got: {result}"
        assert result["bytes"] == len("key: value")

        calls = mock_run.call_args_list
        # Find the docker exec -i ... cat > call (upload pipe)
        upload_cmd = None
        for c in calls:
            cmd = c.args[0]
            if "exec" in cmd and "-i" in cmd:
                upload_cmd = cmd
                break
        assert upload_cmd is not None, f"No docker exec -i call found in: {calls}"
        assert upload_cmd[0] == "docker"
        assert "abc123container" in upload_cmd
        # The sh -c command should contain cat > and the remote path
        sh_cmd = upload_cmd[-1]  # last arg is the sh -c command string
        assert "cat >" in sh_cmd
        assert "/etc/app/config.yaml" in sh_cmd

    def test_download_calls_docker_exec(self, tmp_path):
        """Download via Docker should pipe file content from docker exec cat remote."""
        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(tmp_path / "export.csv"),
                "remote_path": "/data/export.csv",
                "direction": "download",
            }, task_id="test"))

        assert result.get("status") == "ok"

        calls = mock_run.call_args_list
        # Download uses docker exec container cat remote_path (no -i)
        dl_cmd = calls[-1].args[0]
        assert dl_cmd[0] == "docker"
        assert "exec" in dl_cmd
        assert "abc123container" in dl_cmd
        assert "cat" in dl_cmd
        assert "/data/export.csv" in dl_cmd

    def test_existing_container_uses_container_name(self, tmp_path):
        """ExistingDockerEnvironment should use _container name in docker exec."""
        local_file = tmp_path / "key.pem"
        local_file.write_text("PRIVATE KEY DATA")

        env = _make_mock_existing_docker_env()
        _register_mock_env("myapp", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            json.loads(handle_env_file_transfer({
                "env_slug": "myapp",
                "local_path": str(local_file),
                "remote_path": "/root/.ssh/id_rsa",
                "direction": "upload",
            }, task_id="test"))

        calls = mock_run.call_args_list
        # Find the upload exec call — should use "running-app" (container name)
        for c in calls:
            cmd = c.args[0]
            if "exec" in cmd and "-i" in cmd:
                assert "running-app" in cmd, f"Expected container name 'running-app', got: {cmd}"
                return
        assert False, "No docker exec -i call found"

    def test_docker_mkdir_parent_before_exec(self, tmp_path):
        """Upload should docker exec mkdir -p parent dir before file transfer."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("data")

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/new/deep/path/test.txt",
                "direction": "upload",
            }, task_id="test"))

        calls = mock_run.call_args_list
        # First call should be docker exec mkdir -p
        mkdir_cmd = calls[0].args[0]
        assert "docker" in mkdir_cmd[0]
        assert "exec" in mkdir_cmd
        assert "abc123container" in mkdir_cmd
        assert "mkdir" in mkdir_cmd
        assert "/new/deep/path" in mkdir_cmd


# ---------------------------------------------------------------------------
# Secret safety tests
# ---------------------------------------------------------------------------

class TestSecretSafety:
    def test_file_content_not_in_scp_args(self, tmp_path):
        """File content must NOT appear in subprocess args (scp command list)."""
        secret_content = "SUPER_SECRET_TOKEN=abc123"
        local_file = tmp_path / ".env"
        local_file.write_text(secret_content)

        env = _make_mock_ssh_env(tmp_path)
        _register_mock_env("serverA", env, "ssh")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "serverA",
                "local_path": str(local_file),
                "remote_path": "/app/.env",
                "direction": "upload",
            }, task_id="test"))

        assert result.get("status") == "ok"

        # Verify secret content is NOT in any subprocess call args
        for c in mock_run.call_args_list:
            cmd_str = " ".join(str(a) for a in c.args[0])
            assert secret_content not in cmd_str, \
                f"Secret content leaked into subprocess args: {cmd_str}"

    def test_file_content_not_in_docker_exec_args(self, tmp_path):
        """File content must NOT appear in docker exec args."""
        secret_content = "AWS_SECRET_KEY=topsecret"
        local_file = tmp_path / "creds.txt"
        local_file.write_text(secret_content)

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/app/creds.txt",
                "direction": "upload",
            }, task_id="test"))

        assert result.get("status") == "ok"

        for c in mock_run.call_args_list:
            cmd_str = " ".join(str(a) for a in c.args[0])
            assert secret_content not in cmd_str, \
                f"Secret content leaked into docker exec args: {cmd_str}"

    def test_file_content_not_in_response(self, tmp_path):
        """Response JSON must NOT contain file content."""
        secret_content = "DATABASE_URL=postgres://user:pass@host/db"
        local_file = tmp_path / "config.env"
        local_file.write_text(secret_content)

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result_str = handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/app/config.env",
                "direction": "upload",
            }, task_id="test")

        assert secret_content not in result_str, \
            "Secret content leaked into response JSON"


# ---------------------------------------------------------------------------
# Large file warning test
# ---------------------------------------------------------------------------

class TestLargeFile:
    def test_large_file_warning_in_response(self, tmp_path):
        """Files >50MB should get a warning in response but still transfer."""
        local_file = tmp_path / "big.bin"
        # Create a file >50MB (write 51MB)
        local_file.write_bytes(b"\x00" * (51 * 1024 * 1024))

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/tmp/big.bin",
                "direction": "upload",
            }, task_id="test"))

        assert result.get("status") == "ok"
        assert "warning" in result, f"Expected warning for large file, got: {result}"
        assert "50MB" in result["warning"] or "limit" in result["warning"].lower()

    def test_small_file_no_warning(self, tmp_path):
        """Files <50MB should NOT get a warning."""
        local_file = tmp_path / "small.txt"
        local_file.write_text("small file")

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/tmp/small.txt",
                "direction": "upload",
            }, task_id="test"))

        assert result.get("status") == "ok"
        assert "warning" not in result, f"Unexpected warning for small file: {result}"


# ---------------------------------------------------------------------------
# Default direction test
# ---------------------------------------------------------------------------

class TestDefaultDirection:
    def test_default_direction_is_upload(self, tmp_path):
        """Omitting direction should default to upload."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("data")

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/tmp/test.txt",
                # direction omitted
            }, task_id="test"))

        assert result.get("status") == "ok"
        assert result["direction"] == "upload"


# ---------------------------------------------------------------------------
# Transfer failure tests
# ---------------------------------------------------------------------------

class TestTransferFailures:
    def test_scp_failure_returns_error(self, tmp_path):
        """scp returning non-zero should produce error JSON."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("data")

        env = _make_mock_ssh_env(tmp_path)
        _register_mock_env("serverA", env, "ssh")

        with patch("subprocess.run") as mock_run:
            # mkdir succeeds, scp fails
            mock_run.side_effect = [
                MagicMock(returncode=0, stderr=b""),
                MagicMock(returncode=1, stderr=b"Permission denied"),
            ]

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "serverA",
                "local_path": str(local_file),
                "remote_path": "/tmp/test.txt",
                "direction": "upload",
            }, task_id="test"))

        assert "error" in result
        assert "scp" in result["error"].lower() or "permission" in result["error"].lower()

    def test_docker_exec_failure_returns_error(self, tmp_path):
        """docker exec returning non-zero should produce error JSON."""
        local_file = tmp_path / "test.txt"
        local_file.write_text("data")

        env = _make_mock_docker_env()
        _register_mock_env("containerC", env, "docker")

        with patch("subprocess.run") as mock_run:
            # mkdir succeeds, exec upload fails, chmod not reached
            mock_run.side_effect = [
                MagicMock(returncode=0, stderr=b""),  # mkdir
                MagicMock(returncode=1, stderr=b"No such container"),  # exec upload
            ]

            from multienv.transfer import handle_env_file_transfer
            result = json.loads(handle_env_file_transfer({
                "env_slug": "containerC",
                "local_path": str(local_file),
                "remote_path": "/tmp/test.txt",
                "direction": "upload",
            }, task_id="test"))

        assert "error" in result
        assert "docker exec" in result["error"].lower() or "container" in result["error"].lower() or "transfer" in result["error"].lower()


# ---------------------------------------------------------------------------
# Plugin registration test
# ---------------------------------------------------------------------------

class TestPluginRegistration:
    def test_five_tools_registered_including_env_file_transfer(self):
        """register(ctx) should register 5 tools, including env_file_transfer."""
        from multienv import register
        from multienv.schemas import ENV_FILE_TRANSFER_SCHEMA

        class FakeCtx:
            def __init__(self):
                self.registered_tools = []
                self.hooks = []

            def register_tool(self, **kwargs):
                self.registered_tools.append(kwargs)

            def register_hook(self, name, handler):
                self.hooks.append((name, handler))

        ctx = FakeCtx()
        register(ctx)

        assert len(ctx.registered_tools) == 5, \
            f"Expected 5 tools, got {len(ctx.registered_tools)}: {[t['name'] for t in ctx.registered_tools]}"
        tool_names = {t["name"] for t in ctx.registered_tools}
        assert "env_file_transfer" in tool_names
        assert tool_names == {
            "env_connect", "env_list", "env_tool", "env_disconnect", "env_file_transfer"
        }

        # All under multienv toolset
        for t in ctx.registered_tools:
            assert t["toolset"] == "multienv"


# ---------------------------------------------------------------------------
# Docker E2E tests (skip if no Docker)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("docker"), reason="Docker not available")
class TestDockerE2E:
    def test_upload_download_roundtrip(self, tmp_path):
        """Upload a file to Docker, download it back, verify content matches."""
        from multienv.handlers import handle_env_connect, handle_env_disconnect
        from multienv.transfer import handle_env_file_transfer

        # 1. Connect to Docker
        result = json.loads(handle_env_connect({
            "slug": "transfertbox",
            "type": "docker",
            "image": "python:3.12-slim",
            "cwd": "/root",
        }, task_id="test"))
        assert result.get("status") == "ok", f"connect failed: {result}"

        try:
            # 2. Create a local file with known content
            local_file = tmp_path / "upload_test.txt"
            test_content = "Hello from env_file_transfer!\nSecret: TOKEN_12345\n"
            local_file.write_text(test_content)

            # 3. Upload
            result = json.loads(handle_env_file_transfer({
                "env_slug": "transfertbox",
                "local_path": str(local_file),
                "remote_path": "/tmp/uploaded.txt",
                "direction": "upload",
            }, task_id="test"))
            assert result.get("status") == "ok", f"upload failed: {result}"
            assert result["bytes"] == len(test_content)

            # 4. Download to a different local path
            downloaded = tmp_path / "downloaded.txt"
            result = json.loads(handle_env_file_transfer({
                "env_slug": "transfertbox",
                "local_path": str(downloaded),
                "remote_path": "/tmp/uploaded.txt",
                "direction": "download",
            }, task_id="test"))
            assert result.get("status") == "ok", f"download failed: {result}"

            # 5. Verify content matches
            assert downloaded.read_text() == test_content, \
                f"Content mismatch: expected {test_content!r}, got {downloaded.read_text()!r}"

        finally:
            handle_env_disconnect({"slug": "transfertbox"}, task_id="test")

    def test_upload_to_deep_path(self, tmp_path):
        """Upload to a deeply nested path that doesn't exist yet."""
        from multienv.handlers import handle_env_connect, handle_env_disconnect
        from multienv.transfer import handle_env_file_transfer

        result = json.loads(handle_env_connect({
            "slug": "deeptest",
            "type": "docker",
            "image": "python:3.12-slim",
            "cwd": "/root",
        }, task_id="test"))
        assert result.get("status") == "ok"

        try:
            local_file = tmp_path / "deep.txt"
            local_file.write_text("deep content")

            result = json.loads(handle_env_file_transfer({
                "env_slug": "deeptest",
                "local_path": str(local_file),
                "remote_path": "/a/b/c/d/e/deep.txt",
                "direction": "upload",
            }, task_id="test"))
            assert result.get("status") == "ok", f"upload to deep path failed: {result}"

            # Verify file exists via terminal
            from multienv.handlers import handle_env_tool
            result = json.loads(handle_env_tool({
                "env_slug": "deeptest",
                "tool_name": "terminal",
                "args": {"command": "cat /a/b/c/d/e/deep.txt"},
            }, task_id="test"))
            assert "deep content" in result.get("output", "")

        finally:
            handle_env_disconnect({"slug": "deeptest"}, task_id="test")

    def test_permissions_preserved(self, tmp_path):
        """Uploaded file should preserve local permissions (docker cp default)."""
        from multienv.handlers import handle_env_connect, handle_env_disconnect, handle_env_tool
        from multienv.transfer import handle_env_file_transfer

        result = json.loads(handle_env_connect({
            "slug": "permtest",
            "type": "docker",
            "image": "python:3.12-slim",
            "cwd": "/root",
        }, task_id="test"))
        assert result.get("status") == "ok"

        try:
            local_file = tmp_path / "script.sh"
            local_file.write_text("#!/bin/bash\necho hello\n")
            # Set executable permission
            os.chmod(local_file, 0o755)

            result = json.loads(handle_env_file_transfer({
                "env_slug": "permtest",
                "local_path": str(local_file),
                "remote_path": "/tmp/script.sh",
                "direction": "upload",
            }, task_id="test"))
            assert result.get("status") == "ok", f"upload failed: {result}"

            # Check permissions on remote
            result = json.loads(handle_env_tool({
                "env_slug": "permtest",
                "tool_name": "terminal",
                "args": {"command": "stat -c '%a' /tmp/script.sh"},
            }, task_id="test"))
            perm = result.get("output", "").strip()
            assert perm == "755", \
                f"Expected permissions 755, got {perm}. docker cp should preserve permissions."

        finally:
            handle_env_disconnect({"slug": "permtest"}, task_id="test")

    def test_overwrite_existing_file(self, tmp_path):
        """Uploading to an existing remote path should overwrite without error."""
        from multienv.handlers import handle_env_connect, handle_env_disconnect, handle_env_tool
        from multienv.transfer import handle_env_file_transfer

        result = json.loads(handle_env_connect({
            "slug": "overwrite",
            "type": "docker",
            "image": "python:3.12-slim",
            "cwd": "/root",
        }, task_id="test"))
        assert result.get("status") == "ok"

        try:
            # Create initial remote file
            result = json.loads(handle_env_tool({
                "env_slug": "overwrite",
                "tool_name": "terminal",
                "args": {"command": "echo 'old content' > /tmp/overwrite.txt"},
            }, task_id="test"))
            assert result.get("exit_code") == 0

            # Upload new content
            local_file = tmp_path / "new.txt"
            local_file.write_text("new content")

            result = json.loads(handle_env_file_transfer({
                "env_slug": "overwrite",
                "local_path": str(local_file),
                "remote_path": "/tmp/overwrite.txt",
                "direction": "upload",
            }, task_id="test"))
            assert result.get("status") == "ok", f"overwrite upload failed: {result}"

            # Verify content was overwritten
            result = json.loads(handle_env_tool({
                "env_slug": "overwrite",
                "tool_name": "terminal",
                "args": {"command": "cat /tmp/overwrite.txt"},
            }, task_id="test"))
            assert "new content" in result.get("output", "")
            assert "old content" not in result.get("output", "")

        finally:
            handle_env_disconnect({"slug": "overwrite"}, task_id="test")
