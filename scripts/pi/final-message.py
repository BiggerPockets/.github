#!/usr/bin/env python3
"""Print a pi pass's final assistant message from its JSON event stream.

The Stage 1 review prompt's contract is that the model's final message IS the
findings report, captured verbatim and handed to Stage 2. The Codex harness
honored that with its own `output-file`; pi (--mode json) writes an event stream
instead, so the report has to be lifted back out of it.

Extracting the message rather than asking pi to write the report to a file keeps
that prompt — and therefore its content-derived prompt_version, which Datadog
Prompt Tracking groups runs by — byte-identical across the harness switch. Stage 1
output before and after the switch stays directly comparable on the same prompt
version, which is the whole point of changing one variable at a time.

pi reports the same message twice: once as its own `message_end` event and again
inside the terminal `agent_end` event's transcript. Either is a valid source, so
whichever appears last wins; a run killed before `agent_end` still has its
`message_end` events.

Writes nothing and exits 0 when there is no usable message — a Stage 1 failure
leaves empty findings and Stage 2 proceeds on its own, exactly as it did when
Codex failed. This never fails the review.

Usage: final-message.py <pi-event-stream.jsonl>
"""
import json
import sys


def read_events(path):
    """Parse JSON-lines, tolerating a whole-file JSON array or object, and skipping
    any line that doesn't parse — pi's output format has moved before."""
    try:
        with open(path) as handle:
            text = handle.read()
    except OSError:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def message_text(message):
    """The text of one assistant message, or "" when it carries none. pi's content
    is a list of typed parts; only text parts are part of the report."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "".join(parts).strip()


def final_message(events):
    """The last assistant message carrying text, across both places pi reports
    them. An errored final turn usually has no text, so the last message WITH
    text is preferred over the last message outright — that is the report."""
    texts = []
    for event in events:
        if event.get("type") == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                texts.extend(message_text(m) for m in messages)
            continue
        texts.append(message_text(event.get("message")))
    for text in reversed(texts):
        if text:
            return text
    return ""


def main(argv):
    if len(argv) < 2:
        return 0
    sys.stdout.write(final_message(read_events(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
