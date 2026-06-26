"""Thread-safe registry of named execution environments."""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tools.environments.base import BaseEnvironment
from tools.file_operations import ShellFileOperations

logger = logging.getLogger(__name__)


class EnvironmentRegistry:
    """Thread-safe registry mapping slugs to environment instances.

    Holds three parallel dicts keyed by slug:
      - _envs:      BaseEnvironment instances
      - _file_ops:  ShellFileOperations wrappers
      - _meta:      metadata dicts {type, status, cwd, connected_at}

    All public methods are thread-safe via a single threading.Lock.
    """

    def __init__(self) -> None:
        self._envs: Dict[str, BaseEnvironment] = {}
        self._file_ops: Dict[str, ShellFileOperations] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._active_calls = 0
        self._cleanup_pending = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(
        self,
        slug: str,
        env_type: str,
        env: BaseEnvironment,
        file_ops: ShellFileOperations,
    ) -> str:
        """Register a new environment under *slug*.

        Raises ValueError if *slug* is already registered.
        Returns the slug.
        """
        with self._lock:
            if slug in self._envs:
                raise ValueError(
                    f"Environment '{slug}' already connected. "
                    "Use env_disconnect first or choose a different slug."
                )
            self._envs[slug] = env
            self._file_ops[slug] = file_ops
            self._meta[slug] = {
                "slug": slug,
                "type": env_type,
                "status": "connected",
                "cwd": getattr(env, "cwd", ""),
                "connected_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.info("EnvironmentRegistry: connected '%s' (type=%s)", slug, env_type)
            return slug

    def get(self, slug: str) -> Optional[Tuple[BaseEnvironment, ShellFileOperations, Dict]]:
        """Return (env, file_ops, meta) for *slug*, or None if not found."""
        with self._lock:
            env = self._envs.get(slug)
            if env is None:
                return None
            return env, self._file_ops[slug], self._meta[slug]

    def has(self, slug: str) -> bool:
        """Check whether *slug* is registered."""
        with self._lock:
            return slug in self._envs

    def list_envs(self) -> List[Dict[str, Any]]:
        """Return metadata for all registered environments."""
        with self._lock:
            return list(self._meta.values())

    def disconnect(self, slug: str) -> Optional[Dict[str, Any]]:
        """Remove *slug* from the registry and return its metadata.

        The caller is responsible for calling env.cleanup() on the
        returned environment.  Returns None if *slug* is not found.
        """
        with self._lock:
            env = self._envs.pop(slug, None)
            self._file_ops.pop(slug, None)
            meta = self._meta.pop(slug, None)
            if env is None or meta is None:
                return None
            # Attach env instance so caller can call cleanup()
            meta["env"] = env
            meta["status"] = "disconnected"
            logger.info("EnvironmentRegistry: disconnected '%s'", slug)
            return meta

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def generate_slug(self) -> str:
        """Generate an auto-incrementing slug: 'env-1', 'env-2', ..."""
        with self._lock:
            self._counter += 1
            return f"env-{self._counter}"

    def cleanup_all(self) -> None:
        """Call cleanup() on every registered environment. Used by on_session_end.

        If tool calls are in progress (_active_calls > 0), defers cleanup
        until the last call ends (end_call triggers _try_deferred_cleanup).
        This prevents registry wipe during parallel tool execution.
        """
        import traceback

        with self._lock:
            if self._active_calls > 0:
                self._cleanup_pending = True
                logger.warning(
                    "EnvironmentRegistry: cleanup_all deferred — %d active tool calls in progress",
                    self._active_calls,
                )
                logger.debug("cleanup_all caller stack:\n%s", "".join(traceback.format_stack()))
                return
            slugs = list(self._envs.keys())

        for slug in slugs:
            env = self._envs.get(slug)
            if env is not None:
                try:
                    env.cleanup()
                    logger.info("EnvironmentRegistry: cleanup '%s'", slug)
                except Exception as exc:
                    logger.warning("EnvironmentRegistry: cleanup '%s' failed: %s", slug, exc)

        with self._lock:
            self._envs.clear()
            self._file_ops.clear()
            self._meta.clear()
            self._cleanup_pending = False

    # ------------------------------------------------------------------
    # Active-call tracking — prevents cleanup_all from wiping registry
    # while tool calls are in progress (issue #3)
    # ------------------------------------------------------------------

    def begin_call(self) -> None:
        """Mark the start of a tool call. Prevents cleanup_all from clearing the registry."""
        with self._lock:
            self._active_calls += 1

    def end_call(self) -> None:
        """Mark the end of a tool call. Triggers deferred cleanup if pending."""
        with self._lock:
            self._active_calls -= 1
            if self._active_calls <= 0:
                self._active_calls = 0
                should_cleanup = self._cleanup_pending
            else:
                should_cleanup = False

        if should_cleanup:
            logger.info("EnvironmentRegistry: executing deferred cleanup_all after last active call")
            self.cleanup_all()

    # ------------------------------------------------------------------
    # Context-manager support (useful for tests)
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Remove all entries without calling cleanup(). For tests only."""
        with self._lock:
            self._envs.clear()
            self._file_ops.clear()
            self._meta.clear()
            self._counter = 0
            self._active_calls = 0
            self._cleanup_pending = False


# Module-level singleton shared by all tool handlers.
registry = EnvironmentRegistry()
