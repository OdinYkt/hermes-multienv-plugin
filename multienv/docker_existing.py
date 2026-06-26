"""Execution backend for connecting to an EXISTING Docker container.

Unlike ``DockerEnvironment`` (which creates and manages a container
lifecycle), this class attaches to a container that is already running
and executes commands via ``docker exec``.  It does NOT stop or remove
the container on cleanup — the container's lifecycle is managed externally.
"""

import logging
import shlex
import subprocess
from typing import Optional

from tools.environments.base import BaseEnvironment, _popen_bash

logger = logging.getLogger(__name__)


class ExistingDockerEnvironment(BaseEnvironment):
    """Run commands in an existing Docker container via ``docker exec``.

    The container must already be running.  ``cleanup()`` is a no-op —
    we never stop or remove a container we didn't create.
    """

    def __init__(
        self,
        container: str,
        cwd: str = "/",
        timeout: int = 180,
    ) -> None:
        super().__init__(cwd=cwd, timeout=timeout)
        self._container = container

        # Resolve docker binary (reuses core's cached resolution)
        from tools.environments.docker import find_docker

        self._docker_exe = find_docker()
        if not self._docker_exe:
            raise RuntimeError(
                "Docker is not installed or not in PATH. "
                "Install Docker or set HERMES_DOCKER_BINARY."
            )

        # Verify the container exists and is running
        self._verify_container()

    def _verify_container(self) -> None:
        """Check that *self._container* exists and is running."""
        try:
            result = subprocess.run(
                [self._docker_exe, "inspect",
                 "--format", "{{.State.Running}}", self._container],
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Timed out checking container '{self._container}'"
            )

        if result.returncode != 0:
            raise RuntimeError(
                f"Container '{self._container}' not found. "
                f"docker inspect failed: {result.stderr.strip()}"
            )

        running = result.stdout.strip().lower()
        if running != "true":
            raise RuntimeError(
                f"Container '{self._container}' is not running "
                f"(state: {running}). Start it first with 'docker start {self._container}'."
            )

        # Detect available shell — bash preferred, fallback to sh
        # Alpine/distroless containers only have sh/ash
        try:
            shell_check = subprocess.run(
                [self._docker_exe, "exec", self._container,
                 "sh", "-c", "command -v bash >/dev/null 2>&1 && echo bash || echo sh"],
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
            )
            self._shell = shell_check.stdout.strip() or "sh"
        except Exception:
            self._shell = "sh"

        logger.info(
            "ExistingDockerEnvironment: attached to '%s' (shell=%s)",
            self._container, self._shell,
        )

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: Optional[str] = None,
    ) -> subprocess.Popen:
        """Spawn a bash process inside the existing container."""
        cmd = [self._docker_exe, "exec"]
        if stdin_data is not None:
            cmd.append("-i")

        cmd.append(self._container)

        if login:
            cmd.extend([self._shell, "-l", "-c", cmd_string])
        else:
            cmd.extend([self._shell, "-c", cmd_string])

        return _popen_bash(cmd, stdin_data)

    def init_session(self):
        """Capture login shell environment — skip snapshot for non-bash shells.

        BaseEnvironment.init_session() uses bash-isms (declare -f, alias -p,
        shopt -s expand_aliases) that don't work in sh/ash. For non-bash
        containers, skip the snapshot — _wrap_command will use login shell
        for each command instead of sourcing a snapshot file.
        """
        if self._shell == "bash":
            super().init_session()
        else:
            self._snapshot_ready = False
            logger.info(
                "ExistingDockerEnvironment: skipping session snapshot "
                "for '%s' (shell=%s, not bash)",
                self._container, self._shell,
            )

    def _wrap_command(self, command: str, cwd: str) -> str:
        """Build shell script for command execution.

        For non-bash shells (sh/ash), overrides BaseEnvironment._wrap_command
        to avoid bash-isms: 'builtin cd', 'source', 'declare', 'shopt'.
        Uses plain POSIX sh syntax instead.
        """
        if self._shell == "bash":
            return super()._wrap_command(command, cwd)

        # POSIX sh fallback — no snapshot, plain cd, CWD marker
        import shlex as _shlex
        escaped = command.replace("'", "'\\''")
        _quoted_cwd_file = _shlex.quote(self._cwd_file)

        quoted_cwd = self._quote_cwd_for_cd(cwd)
        parts = [
            f"cd -- {quoted_cwd} || exit 126",
            f"eval '{escaped}'",
            f"__hermes_ec=$?",
            f"pwd -P > {_quoted_cwd_file} 2>/dev/null || true",
            f"printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' \"$(pwd -P)\"",
            f"exit $__hermes_ec",
        ]
        return "\n".join(parts)

    def cleanup(self) -> None:
        """No-op — we don't own the container's lifecycle."""
        logger.info(
            "ExistingDockerEnvironment: detach from '%s' "
            "(container left running)",
            self._container,
        )
