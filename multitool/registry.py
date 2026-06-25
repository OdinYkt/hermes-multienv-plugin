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
        """Call cleanup() on every registered environment. Used by on_session_end."""
        with self._lock:
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


# Module-level singleton shared by all tool handlers.
registry = EnvironmentRegistry()
