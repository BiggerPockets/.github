#!/usr/bin/env python3
"""Save terminal review errors without publishing tool transcripts."""
import html
import json
import os
from pathlib import Path
import sys

FIELDS = ('type', 'subtype', 'is_error', 'result', 'errors', 'duration_ms',
          'num_turns', 'total_cost_usd', 'session_id')


def read_result(source):
    try:
        text = source.read_text()
        try:
            messages = json.loads(text)
        except json.JSONDecodeError:
            messages = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (OSError, ValueError):
        return {'status': 'execution output unavailable'}
    if isinstance(messages, dict):
        messages = [messages]
    if not isinstance(messages, list):
        return {'status': 'result unavailable'}
    results = [message for message in messages
               if isinstance(message, dict) and message.get('type') == 'result']
    if not results:
        return {'status': 'result unavailable'}
    return {'status': 'captured', 'result': {
        key: results[-1][key] for key in FIELDS if key in results[-1]
    }}


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
    source, destination = map(Path, sys.argv[1:])
    secrets = json.loads(os.environ.get('DIAGNOSTIC_SECRET_VALUES', '[]'))
    secrets += [value for key, value in os.environ.items()
                if key.startswith('DIAGNOSTIC_TOKEN_') and value]
    secrets = sorted(set(filter(None, secrets)), key=len, reverse=True)
    report = redact(read_result(source), secrets)
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
    main()
