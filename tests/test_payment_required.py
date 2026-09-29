import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pi/payment-required.py'
spec = importlib.util.spec_from_file_location('payment_required', SCRIPT)
payment_required = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payment_required)


def write(name, text):
    path = Path(tempfile.mkdtemp()) / name
    path.write_text(text)
    return str(path)


def stream(*messages):
    return write('pi-output.jsonl', '\n'.join(
        json.dumps({'type': 'message_end', 'message': m}) for m in messages))


def assistant(stop_reason='stop', error=None, text=None):
    message = {'role': 'assistant', 'stopReason': stop_reason}
    if error:
        message['errorMessage'] = error
    if text:
        message['content'] = [{'type': 'text', 'text': text}]
    return message


class PaymentRequiredTest(unittest.TestCase):
    def test_true_when_the_pass_ended_on_a_402(self):
        path = stream(assistant(text='looking'),
                      assistant('error', '402 Payment Required: insufficient credits'))
        self.assertTrue(payment_required.payment_required(path))

    def test_false_for_any_other_error(self):
        path = stream(assistant('error', '429 Too Many Requests'))
        self.assertFalse(payment_required.payment_required(path))

    def test_false_when_a_402_was_recovered_from(self):
        path = stream(assistant('error', '402 Payment Required'),
                      assistant(text='the report'))
        self.assertFalse(payment_required.payment_required(path))

    def test_reads_the_last_message_of_the_agent_end_transcript(self):
        path = write('pi-output.jsonl', json.dumps({'type': 'agent_end', 'messages': [
            assistant(text='looking'), assistant('error', 'Payment Required')]}))
        self.assertTrue(payment_required.payment_required(path))

    def test_true_when_only_stderr_reports_it(self):
        path = stream(assistant(text='partial'))
        stderr = write('pi-error.log', 'Error: 402 This request requires more credits')
        self.assertTrue(payment_required.payment_required(path, stderr))

    def test_false_when_nothing_was_written(self):
        self.assertFalse(payment_required.payment_required('/nonexistent.jsonl'))


if __name__ == '__main__':
    unittest.main()
