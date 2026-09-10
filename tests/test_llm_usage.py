import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/llm-usage.py'
spec = importlib.util.spec_from_file_location('llm_usage', SCRIPT)
llm_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm_usage)


def write(content, name='execution.json'):
    path = Path(tempfile.mkdtemp()) / name
    path.write_text(content)
    return str(path)


def write_rollout(events, name='rollout-abc.jsonl', subdir='2026/09/10'):
    directory = Path(tempfile.mkdtemp()) / subdir
    directory.mkdir(parents=True)
    (directory / name).write_text('\n'.join(json.dumps(e) for e in events))
    return str(Path(directory).parents[2])


def token_count(**usage):
    return {'type': 'event_msg',
            'payload': {'type': 'token_count', 'info': {'total_token_usage': usage}}}


def pi_message(stop_reason='stop', usage=None, text='', error=None, timestamp=2000):
    message = {'role': 'assistant', 'stopReason': stop_reason, 'timestamp': timestamp}
    if usage is not None:
        message['usage'] = usage
    if error:
        message['errorMessage'] = error
    elif text:
        message['content'] = [{'type': 'text', 'text': text}]
    return message


def pi_stream(*messages):
    """A minimal pi event stream: session, turn_start, one message_end per
    assistant message, then turn_end and agent_end carrying all of them (the last
    message is the terminal one)."""
    events = [{'type': 'session'}, {'type': 'turn_start'}]
    events += [{'type': 'message_end', 'message': message}
               for message in messages]
    events += [{'type': 'turn_end'},
               {'type': 'agent_end', 'messages': list(messages)}]
    return events


class SplitModelTest(unittest.TestCase):
    def test_splits_openrouter_slug_into_catalog_name_and_provider(self):
        self.assertEqual(llm_usage.split_model('openai/gpt-5.6-sol'),
                         ('gpt-5.6-sol', 'openai'))

    def test_bare_model_reports_unspecified_provider(self):
        self.assertEqual(llm_usage.split_model('gpt-5.6-sol'),
                         ('gpt-5.6-sol', 'unspecified'))

    def test_empty_slug_is_fully_unspecified(self):
        self.assertEqual(llm_usage.split_model(''), ('unspecified', 'unspecified'))
        self.assertEqual(llm_usage.split_model(None), ('unspecified', 'unspecified'))


class OpenrouterCostTest(unittest.TestCase):
    def test_reads_the_charged_amount(self):
        self.assertEqual(llm_usage.openrouter_cost({'cost': 0.0142}), 0.0142)

    def test_falls_back_to_the_upstream_share_when_only_the_breakdown_survives(self):
        usage = {'cost_details': {'upstream_inference_cost': 0.009}}
        self.assertEqual(llm_usage.openrouter_cost(usage), 0.009)

    def test_prefers_the_total_charge_over_the_upstream_share(self):
        usage = {'cost': 0.0142, 'cost_details': {'upstream_inference_cost': 0.009}}
        self.assertEqual(llm_usage.openrouter_cost(usage), 0.0142)

    def test_absent_cost_is_none_not_zero(self):
        self.assertIsNone(llm_usage.openrouter_cost({'input_tokens': 10}))
        self.assertIsNone(llm_usage.openrouter_cost(None))

    def test_rejects_a_non_numeric_cost(self):
        self.assertIsNone(llm_usage.openrouter_cost({'cost': 'free'}))
        self.assertIsNone(llm_usage.openrouter_cost({'cost': True}))


class ClaudeUsageTest(unittest.TestCase):
    def test_reads_counts_and_reported_cost_from_the_result_message(self):
        path = write(json.dumps([
            {'type': 'assistant'},
            {'type': 'result', 'usage': {'input_tokens': 1000, 'output_tokens': 200,
                                         'cache_read_input_tokens': 800,
                                         'cache_creation_input_tokens': 50,
                                         'cost': 0.0031}},
        ]))
        counts, cost = llm_usage.claude_usage(path)
        self.assertEqual(counts, {'input_tokens': 1000, 'output_tokens': 200,
                                  'cache_read_input_tokens': 800,
                                  'cache_write_input_tokens': 50})
        self.assertEqual(cost, 0.0031)

    def test_ignores_claude_codes_own_cost_estimate(self):
        path = write(json.dumps([{'type': 'result', 'total_cost_usd': 9.99,
                                  'usage': {'input_tokens': 5}}]))
        _, cost = llm_usage.claude_usage(path)
        self.assertIsNone(cost)

    def test_takes_the_last_result_message(self):
        path = write('\n'.join([
            json.dumps({'type': 'result', 'usage': {'input_tokens': 1}}),
            json.dumps({'type': 'result', 'usage': {'input_tokens': 7}}),
        ]))
        counts, _ = llm_usage.claude_usage(path)
        self.assertEqual(counts['input_tokens'], 7)

    def test_missing_file_yields_nothing(self):
        self.assertEqual(llm_usage.claude_usage('/nonexistent/execution.json'), ({}, None))
        self.assertEqual(llm_usage.claude_usage(''), ({}, None))

    def test_unparseable_content_yields_nothing(self):
        self.assertEqual(llm_usage.claude_usage(write('not json at all')), ({}, None))

    def test_skips_non_numeric_counts(self):
        path = write(json.dumps({'type': 'result', 'usage': {
            'input_tokens': 'lots', 'output_tokens': True,
            'cache_read_input_tokens': 5}}))
        counts, _ = llm_usage.claude_usage(path)
        self.assertEqual(counts, {'cache_read_input_tokens': 5})


class CodexUsageTest(unittest.TestCase):
    def test_reads_the_running_totals_from_the_rollout(self):
        directory = write_rollout([
            token_count(input_tokens=100, output_tokens=10),
            token_count(input_tokens=31751, cached_input_tokens=14720,
                        output_tokens=2367, total_tokens=34118),
        ])
        counts, cost = llm_usage.codex_usage(directory)
        self.assertEqual(counts, {'input_tokens': 31751, 'output_tokens': 2367,
                                  'cache_read_input_tokens': 14720})
        self.assertIsNone(cost)

    def test_takes_the_most_recent_rollout_when_several_exist(self):
        directory = write_rollout([token_count(input_tokens=1)])
        newer = Path(directory) / '2026/09/11'
        newer.mkdir(parents=True)
        path = newer / 'rollout-def.jsonl'
        path.write_text(json.dumps(token_count(input_tokens=42)))
        os.utime(path, (2_000_000_000, 2_000_000_000))
        counts, _ = llm_usage.codex_usage(directory)
        self.assertEqual(counts['input_tokens'], 42)

    def test_rollout_without_token_counts_yields_nothing(self):
        directory = write_rollout([{'type': 'event_msg', 'payload': {'type': 'agent_message'}}])
        self.assertEqual(llm_usage.codex_usage(directory), ({}, None))

    def test_missing_session_directory_yields_nothing(self):
        self.assertEqual(llm_usage.codex_usage('/nonexistent/sessions'), ({}, None))
        self.assertEqual(llm_usage.codex_usage(''), ({}, None))


class PiUsageTest(unittest.TestCase):
    def test_reads_counts_and_computed_cost_from_the_last_finished_message(self):
        usage = {'input': 17515, 'output': 17, 'cacheRead': 17024,
                 'cacheWrite': 0, 'totalTokens': 34556,
                 'cost': {'input': 0.000332785, 'output': 5.1e-07,
                          'cacheRead': 0.000038966, 'cacheWrite': 0,
                          'total': 0.000372255}}
        path = write('\n'.join(json.dumps(e) for e in pi_stream(pi_message(usage=usage))))
        counts, cost = llm_usage.pi_usage(path)
        self.assertEqual(counts, {'input_tokens': 17515, 'output_tokens': 17,
                                  'cache_read_input_tokens': 17024,
                                  'cache_write_input_tokens': 0})
        self.assertEqual(cost, 0.000372255)

    def test_prefers_the_last_clean_message_over_a_failed_retry_after_it(self):
        clean = pi_message(stop_reason='stop', text='review done', usage={
            'input': 500, 'output': 100, 'cacheRead': 0, 'cacheWrite': 0,
            'totalTokens': 600, 'cost': {'total': 0.9}})
        failed = pi_message(stop_reason='error', error='429 rate limited', usage={
            'input': 10, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0,
            'totalTokens': 10, 'cost': {'total': 0.0}})
        path = write('\n'.join(json.dumps(e) for e in pi_stream(clean, failed)))
        counts, cost = llm_usage.pi_usage(path)
        self.assertEqual(counts['input_tokens'], 500)
        self.assertEqual(cost, 0.9)

    def test_zero_cost_is_no_reported_cost(self):
        usage = {'input': 10, 'output': 2, 'cacheRead': 0, 'cacheWrite': 0,
                 'totalTokens': 12, 'cost': {'total': 0.0}}
        path = write('\n'.join(json.dumps(e) for e in pi_stream(pi_message(usage=usage))))
        _, cost = llm_usage.pi_usage(path)
        self.assertIsNone(cost)

    def test_missing_file_yields_nothing(self):
        self.assertEqual(llm_usage.pi_usage('/nonexistent/pi-output.jsonl'), ({}, None))
        self.assertEqual(llm_usage.pi_usage(''), ({}, None))

    def test_stream_without_an_assistant_message_yields_nothing(self):
        path = write('\n'.join(json.dumps(e) for e in [
            {'type': 'session'}, {'type': 'turn_start'},
            {'type': 'agent_end', 'messages': []},
        ]))
        self.assertEqual(llm_usage.pi_usage(path), ({}, None))

    def test_agent_end_alone_is_enough_when_no_message_end_events_exist(self):
        events = [{'type': 'session'},
                  {'type': 'agent_end', 'messages': [
                      pi_message(usage={'input': 7, 'output': 3, 'cacheRead': 0,
                                        'cacheWrite': 0, 'totalTokens': 10,
                                        'cost': {'total': 0.01}})]}]
        path = write('\n'.join(json.dumps(e) for e in events))
        counts, cost = llm_usage.pi_usage(path)
        self.assertEqual(counts['input_tokens'], 7)
        self.assertEqual(cost, 0.01)


class BuildSpanFieldsTest(unittest.TestCase):
    def test_reports_catalog_identity_and_derived_total(self):
        fields = llm_usage.build_span_fields(
            'openai/gpt-5.6-sol', {'input_tokens': 1000, 'output_tokens': 200}, None)
        self.assertEqual(fields['model_name'], 'gpt-5.6-sol')
        self.assertEqual(fields['model_provider'], 'openai')
        self.assertEqual(fields['gateway'], 'openrouter')
        self.assertEqual(fields['metrics']['total_tokens'], 1200)

    def test_omits_cost_when_none_was_reported(self):
        fields = llm_usage.build_span_fields(
            'openai/gpt-5.6-sol', {'input_tokens': 1000, 'output_tokens': 200}, None)
        self.assertNotIn('total_cost', fields['metrics'])

    def test_reports_a_reported_cost_under_datadogs_metric_name(self):
        fields = llm_usage.build_span_fields(
            'deepseek/deepseek-v4.1-flash', {'input_tokens': 1000}, 0.0031)
        self.assertEqual(fields['metrics']['total_cost'], 0.0031)

    def test_omits_the_derived_total_when_a_count_is_missing(self):
        fields = llm_usage.build_span_fields('x/y', {'input_tokens': 1000}, None)
        self.assertNotIn('total_tokens', fields['metrics'])

    def test_no_usage_at_all_still_yields_a_usable_span(self):
        fields = llm_usage.build_span_fields('openai/gpt-5.6-sol', {}, None)
        self.assertEqual(fields['metrics'], {})
        self.assertEqual(fields['model_name'], 'gpt-5.6-sol')


class MainTest(unittest.TestCase):
    def test_unknown_pass_name_emits_an_empty_but_valid_object(self):
        llm_usage.main(['llm-usage.py', 'nonsense', 'openai/gpt-5.6-sol'])

    def test_exits_zero_with_no_arguments(self):
        self.assertEqual(llm_usage.main(['llm-usage.py']), 0)


if __name__ == '__main__':
    unittest.main()
