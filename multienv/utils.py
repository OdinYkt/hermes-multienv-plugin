"""Utility helpers for multienv plugin."""

import json
from typing import Any, Dict, Optional


def format_success(data: Dict[str, Any]) -> str:
    """Serialize a success response as JSON with status=ok."""
    payload = {"status": "ok"}
    payload.update(data)
    return json.dumps(payload, ensure_ascii=False)


def format_error(msg: str, **extra: Any) -> str:
    """Serialize an error response as JSON."""
    payload: Dict[str, Any] = {"error": msg}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Output post-processing (mirrors core execute_code behaviour)
# ---------------------------------------------------------------------------

MAX_STDOUT_BYTES = 50_000


def truncate_output(output: str, max_bytes: int = MAX_STDOUT_BYTES) -> str:
    """Truncate long output to *max_bytes* using head + tail + omission notice."""
    if len(output) <= max_bytes:
        return output
    head_bytes = int(max_bytes * 0.4)
    tail_bytes = max_bytes - head_bytes
    head = output[:head_bytes]
    tail = output[-tail_bytes:]
    omitted = len(output) - len(head) - len(tail)
    return (
        head
        + f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted "
        f"out of {len(output):,} total] ...\n\n"
        + tail
    )


def strip_ansi_and_redact(output: str) -> str:
    """Strip ANSI escape sequences and redact secrets from *output*."""
    try:
        from tools.ansi_strip import strip_ansi

        output = strip_ansi(output)
    except Exception:
        pass

    try:
        from agent.redact import redact_sensitive_text

        output = redact_sensitive_text(output)
    except Exception:
        pass

    return output


def postprocess_output(output: str) -> str:
    """Apply truncation + ANSI strip + secret redaction in one call."""
    return strip_ansi_and_redact(truncate_output(output))
