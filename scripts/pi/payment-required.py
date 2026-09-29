#!/usr/bin/env python3
"""Exit 0 when a pi pass ended on OpenRouter's "402 Payment Required", 1 otherwise.

OpenRouter reserves credit against every in-flight request, so a 402 means the account
was momentarily over-committed, not that the model failed: the same request succeeds
once other requests finish. A caller uses this to tell that apart from a real failure
and retry the pass.

pi reports a failed API call as a final assistant message with stopReason "error" and
the provider's message in errorMessage, and may also write it to stderr. Both are
checked. Nothing is printed, since either can quote the model's partial output.

Usage: payment-required.py <pi-event-stream.jsonl> [<pi-stderr.log>]
"""
import json
import re
import sys

PAYMENT_REQUIRED = re.compile(r"\b402\b|payment required|insufficient credits", re.I)


def last_assistant(path):
    """The last assistant message in a pi event stream, or None."""
    last = None
    try:
        with open(path) as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        messages = event.get("messages") if event.get("type") == "agent_end" else None
        candidates = messages if isinstance(messages, list) else [event.get("message")]
        for message in candidates:
            if isinstance(message, dict) and message.get("role") == "assistant":
                last = message
    return last


def payment_required(stream_path, stderr_path=None):
    message = last_assistant(stream_path)
    if message and message.get("stopReason") == "error" \
            and PAYMENT_REQUIRED.search(str(message.get("errorMessage") or "")):
        return True
    if stderr_path:
        try:
            with open(stderr_path, errors="replace") as handle:
                return bool(PAYMENT_REQUIRED.search(handle.read()))
        except OSError:
            pass
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)
    sys.exit(0 if payment_required(*sys.argv[1:3]) else 1)
