"""Hermes plugin shim — re-exports register() from multienv package.

This file exists so that Hermes directory-based plugin discovery
finds both ``plugin.yaml`` and ``__init__.py`` at the repository root.
The actual plugin code lives in the ``multienv/`` subdirectory.
"""

from multienv import register, _on_session_end  # noqa: F401
