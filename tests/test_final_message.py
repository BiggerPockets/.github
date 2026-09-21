import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pi/final-message.py'
spec = importlib.util.spec_from_file_location('final_message', SCRIPT)
final_message = importlib.util.module_from_spec(spec)
spec.loader.exec_module(final_message)


def write(events):
    path = Path(tempfile.mkdtemp()) / 'pi-output.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events))
    return str(path)


def assistant(text=None, stop_reason='stop', error=None):
    message = {'role': 'assistant', 'stopReason': stop_reason}
    if error:
        message['errorMessage'] = error
    if text is not None:
        message['content'] = [{'type': 'text', 'text': text}]
    return message


class MessageTextTest(unittest.TestCase):
    def test_joins_the_text_parts_of_a_message(self):
        message = {'role': 'assistant', 'content': [
            {'type': 'text', 'text': '## Findings\n'},
            {'type': 'text', 'text': 'No blocking issues.'},
        ]}
        self.assertEqual(final_message.message_text(message),
                         '## Findings\nNo blocking issues.')

    def test_ignores_non_text_parts(self):
        message = {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'name': 'grep'},
            {'type': 'text', 'text': 'report'},
        ]}
        self.assertEqual(final_message.message_text(message), 'report')

    def test_ignores_messages_that_are_not_the_assistant_s(self):
        self.assertEqual(final_message.message_text(
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}), '')

    def test_accepts_plain_string_content(self):
        self.assertEqual(final_message.message_text(
            {'role': 'assistant', 'content': 'report'}), 'report')

    def test_empty_for_a_message_with_no_content(self):
        self.assertEqual(final_message.message_text({'role': 'assistant'}), '')
        self.assertEqual(final_message.message_text(None), '')


class FinalMessageTest(unittest.TestCase):
    def test_takes_the_last_assistant_message_with_text(self):
        events = [
            {'type': 'message_end', 'message': assistant('first pass thinking')},
            {'type': 'message_end', 'message': assistant('## Findings\nThe report.')},
        ]
        self.assertEqual(final_message.final_message(events), '## Findings\nThe report.')

    def test_reads_the_transcript_when_only_agent_end_carries_it(self):
        events = [{'type': 'agent_end', 'messages': [assistant('## Findings\nThe report.')]}]
        self.assertEqual(final_message.final_message(events), '## Findings\nThe report.')

    def test_survives_a_run_killed_before_agent_end(self):
        # A timed-out Stage 1 still produced findings worth handing to Stage 2.
        events = [{'type': 'message_end', 'message': assistant('## Findings\npartial')}]
        self.assertEqual(final_message.final_message(events), '## Findings\npartial')

    def test_prefers_the_last_message_that_actually_has_text(self):
        # A failed final turn carries an error and no text; the report is the
        # message before it, not an empty string.
        events = [
            {'type': 'message_end', 'message': assistant('## Findings\nThe report.')},
            {'type': 'message_end', 'message': assistant(None, stop_reason='error',
                                                         error='429 rate limited')},
        ]
        self.assertEqual(final_message.final_message(events), '## Findings\nThe report.')

    def test_empty_when_the_pass_produced_nothing(self):
        self.assertEqual(final_message.final_message([]), '')
        self.assertEqual(final_message.final_message(
            [{'type': 'session'}, {'type': 'agent_end', 'messages': []}]), '')


class ReadEventsTest(unittest.TestCase):
    def test_reads_json_lines(self):
        path = write([{'type': 'message_end', 'message': assistant('report')}])
        self.assertEqual(final_message.final_message(final_message.read_events(path)), 'report')

    def test_skips_unparseable_lines(self):
        path = Path(tempfile.mkdtemp()) / 'pi-output.jsonl'
        path.write_text('not json\n' + json.dumps(
            {'type': 'message_end', 'message': assistant('report')}))
        self.assertEqual(final_message.final_message(final_message.read_events(str(path))), 'report')

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(final_message.read_events('/nonexistent/pi-output.jsonl'), [])


class MainTest(unittest.TestCase):
    def test_exits_zero_with_no_arguments(self):
        self.assertEqual(final_message.main(['final-message.py']), 0)

    def test_exits_zero_on_a_missing_file(self):
        self.assertEqual(final_message.main(['final-message.py', '/nonexistent/x.jsonl']), 0)


if __name__ == '__main__':
    unittest.main()
