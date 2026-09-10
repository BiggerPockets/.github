#!/usr/bin/env python3
"""Save terminal review errors without publishing tool transcripts.

Understands the two Stage-2 harness output formats that have existed:

- pi's JSON event stream (`pi --mode json`): the terminal assistant message, its
  stop reason / error, and pass usage are extracted from the stream. Tool calls,
  their arguments and results are never included.
- Claude Code's execution file (historical): terminal "result" messages only.

Also appends a redacted tail of pi's stderr (passed as an optional third
argument) so upstream errors like rate limits show up in diagnostics.

Everything that goes into the report is passed through redact(), which blanks
known secrets (the provider key, the review PAT, the GITHUB_TOKEN) anywhere they
appear.
"""
import html
import json
import os
from pathlib import Path
import sys

FIELDS = ('type', 'subtype', 'is_error', 'result', 'errors', 'duration_ms',
          'num_turns', 'total_cost_usd', 'session_id')

# pi --mode json emits these event types on the wire.
PI_EVENT_TYPES = {'session', 'agent_start', 'agent_end', 'turn_start', 'turn_end',
                  'message_start', 'message_end', 'message_update'}


def read_events(text):
    """Parse a file that is a JSON array, a single JSON object, or JSON-lines into a
    list of dicts. Empty on anything unreadable."""
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


def is_pi_stream(events):
    """A pi event stream announces itself with wire event types."""
    return any(event.get('type') in PI_EVENT_TYPES for event in events)


def pi_result(events):
    """The terminal assistant message from a pi event stream, stripped of tools:
    stop reason, error, model, usage, turns, and (if timestamps survive) duration.
    Tool calls, arguments, and results are never read."""
    candidates = []
    for event in events:
        message = event.get('message')
        if event.get('type') == 'agent_end':
            messages = event.get('messages')
            if isinstance(messages, list) and messages:
                message = messages[-1]
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        candidates.append(message)
    if not candidates:
        return None
    message = candidates[-1]
    text = ' '.join(part.get('text', '')
                    for part in message.get('content', [])
                    if isinstance(part, dict) and part.get('type') == 'text')
    is_error = message.get('stopReason') == 'error'
    result = {
        'subtype': message.get('stopReason'),
        'is_error': is_error,
        'num_turns': sum(1 for e in events if e.get('type') == 'turn_start'),
        'model': message.get('model'),
        'provider': message.get('provider'),
    }
    usage = message.get('usage')
    if isinstance(usage, dict):
        cost = usage.get('cost')
        if isinstance(cost, dict):
            total = cost.get('total')
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                result['total_cost'] = total
        result['usage'] = {key: usage[key] for key in
                           ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens')
                           if key in usage}
    if is_error:
        result['errors'] = ([message.get('errorMessage')]
                            if message.get('errorMessage') else [])
        result['result'] = message.get('errorMessage') or ''
    else:
        result['result'] = text
    stamps = [ts for event in events
              if isinstance(ts := event.get('timestamp'), (int, float))]
    for message in candidates:
        if isinstance(ts := message.get('timestamp'), (int, float)):
            stamps.append(ts)
    if stamps:
        result['duration_ms'] = int(max(stamps) - min(stamps))
    return result


def read_result(source):
    try:
        text = source.read_text()
    except OSError:
        return {'status': 'execution output unavailable'}
    events = read_events(text)
    if not events:
        return {'status': 'execution output unavailable'}
    if is_pi_stream(events):
        result = pi_result(events)
        if result is not None:
            return {'status': 'captured', 'result': result}
        return {'status': 'result unavailable'}
    results = [event for event in events if event.get('type') == 'result']
    if not results:
        return {'status': 'result unavailable'}
    return {'status': 'captured', 'result': {
        key: results[-1][key] for key in FIELDS if key in results[-1]
    }}


def read_stderr_tail(path, max_lines=40):
    """The last lines of pi's captured stderr, or None when absent."""
    if not path:
        return None
    try:
        lines = Path(path).read_text(errors='replace').splitlines()
    except OSError:
        return None
    return lines[-max_lines:] or None


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '[REDACTED]')
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    return value


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print('usage: review-diagnostics.py <execution-output> <destination> [stderr]',
              file=sys.stderr)
        return 2
    source, destination = map(Path, args[:2])
    stderr_path = Path(args[2]) if len(args) > 2 else None
    secrets = json.loads(os.environ.get('DIAGNOSTIC_SECRET_VALUES', '[]'))
    secrets += [value for key, value in os.environ.items()
                if key.startswith('DIAGNOSTIC_TOKEN_') and value]
    secrets = sorted(set(filter(None, secrets)), key=len, reverse=True)
    report = redact(read_result(source), secrets)
    tail = read_stderr_tail(stderr_path)
    if tail is not None:
        report['stderr_tail'] = redact(tail, secrets)
    output = json.dumps(report, indent=2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(output + '\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write('\n### AI review failure diagnostics\n\n')
            stream.write('Terminal result only; tool transcripts are omitted. '
                         'The artifact contains the same sanitized details.\n\n')
            stream.write('<pre>' + html.escape(output[:16000]) + '</pre>\n')


if __name__ == '__main__':
    sys.exit(main())