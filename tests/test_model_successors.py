import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pi/model_successors.py'
spec = importlib.util.spec_from_file_location('model_successors', SCRIPT)
successors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(successors)


def entry(name, created=1790000000, reasoning=True, prompt="0.000001", completion="0.000005"):
    return {"name": name, "created": created,
            "supported_parameters": ["reasoning"] if reasoning else ["tools"],
            "pricing": {"prompt": prompt, "completion": completion}}


MODELS_TEXT = """{
  "//": "first comment",
  "//": "second comment",
  "providers": {
    "openrouter": {
      "models": [
        {
          "id": "openai/gpt-5.6-terra",
          "name": "GPT-5.6 Terra",
          "reasoning": true
        },
        {
          "id": "anthropic/claude-haiku-4.5",
          "name": "Claude Haiku 4.5",
          "reasoning": true
        }
      ]
    }
  }
}
"""


class FindSuccessorsTest(unittest.TestCase):
    def test_finds_the_newest_release_of_the_same_family(self):
        catalog = {
            "openai/gpt-5.6-terra": entry("OpenAI: GPT-5.6 Terra"),
            "openai/gpt-6-terra": entry("OpenAI: GPT-6 Terra"),
            "openai/gpt-6.1-terra": entry("OpenAI: GPT-6.1 Terra"),
        }
        found = successors.find_successors(["openai/gpt-5.6-terra"], catalog)
        self.assertEqual([(s["pinned"], s["successor"]) for s in found],
                         [("openai/gpt-5.6-terra", "openai/gpt-6.1-terra")])
        self.assertEqual(found[0]["name"], "GPT-6.1 Terra")

    def test_compares_versions_numerically(self):
        catalog = {"a/model-4.9": entry("A: Model 4.9"), "a/model-4.10": entry("A: Model 4.10")}
        found = successors.find_successors(["a/model-4.9"], catalog)
        self.assertEqual(found[0]["successor"], "a/model-4.10")

    def test_ignores_other_tiers_snapshots_batch_variants_and_aliases(self):
        catalog = {
            "openai/gpt-5.6-luna": entry("OpenAI: GPT-5.6 Luna"),
            "openai/gpt-6-luna-pro": entry("OpenAI: GPT-6 Luna Pro"),
            "openai/gpt-6-luna:batch": entry("OpenAI: GPT-6 Luna (batch)"),
            "~openai/gpt-luna-latest": entry("OpenAI: GPT Luna Latest"),
            "deepseek/deepseek-v4.1-flash": entry("DeepSeek: V4.1 Flash"),
            "deepseek/deepseek-v4.1-flash-1017": entry("DeepSeek: V4.1 Flash 1017"),
        }
        found = successors.find_successors(
            ["openai/gpt-5.6-luna", "deepseek/deepseek-v4.1-flash"], catalog)
        self.assertEqual(found, [])

    def test_nothing_when_the_newest_release_is_already_pinned(self):
        catalog = {"openai/gpt-5.6-luna": entry("OpenAI: GPT-5.6 Luna"),
                   "openai/gpt-6-luna": entry("OpenAI: GPT-6 Luna")}
        self.assertEqual(successors.find_successors(
            ["openai/gpt-6-luna", "openai/gpt-5.6-luna"], catalog), [])

    def test_nothing_for_an_older_release(self):
        catalog = {"anthropic/claude-sonnet-5.5": entry("Anthropic: Claude Sonnet 5.5"),
                   "anthropic/claude-sonnet-5": entry("Anthropic: Claude Sonnet 5")}
        self.assertEqual(successors.find_successors(["anthropic/claude-sonnet-5.5"], catalog), [])

    def test_reports_release_date_reasoning_and_both_prices(self):
        catalog = {
            "anthropic/claude-haiku-4.5": entry("Anthropic: Claude Haiku 4.5", prompt="0.000001",
                                                completion="0.000005"),
            "anthropic/claude-haiku-5.5": entry("Anthropic: Claude Haiku 5.5", created=1791331200,
                                                reasoning=False, prompt="0.0000001",
                                                completion="0.0000005"),
        }
        found = successors.find_successors(["anthropic/claude-haiku-4.5"], catalog)[0]
        self.assertEqual(found["released"], "2026-10-07")
        self.assertFalse(found["reasoning"])
        self.assertEqual(found["pinned_rates"], {"input": 1.0, "output": 5.0})
        self.assertEqual(found["successor_rates"], {"input": 0.1, "output": 0.5})


class InsertSuccessorsTest(unittest.TestCase):
    SUCCESSOR = {"pinned": "anthropic/claude-haiku-4.5", "successor": "anthropic/claude-haiku-5.5",
                 "name": "Claude Haiku 5.5", "reasoning": True}

    def test_inserts_before_the_entry_it_succeeds_and_keeps_every_comment(self):
        updated = successors.insert_successors(MODELS_TEXT, [self.SUCCESSOR])
        self.assertEqual(updated.count('"//"'), 2)
        ids = successors.load_pinned_ids(updated)
        self.assertEqual(ids, ["openai/gpt-5.6-terra", "anthropic/claude-haiku-5.5",
                               "anthropic/claude-haiku-4.5"])
        self.assertIn('        {\n          "id": "anthropic/claude-haiku-5.5",\n'
                      '          "name": "Claude Haiku 5.5",\n          "reasoning": true\n        },\n'
                      '        {\n          "id": "anthropic/claude-haiku-4.5",', updated)

    def test_raises_when_the_pinned_entry_is_missing(self):
        with self.assertRaises(ValueError):
            successors.insert_successors(MODELS_TEXT, [{**self.SUCCESSOR, "pinned": "x/absent"}])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.original_fetch_catalog = successors.fetch_catalog

    def tearDown(self):
        successors.fetch_catalog = self.original_fetch_catalog

    def run_main(self, *args):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = successors.main(['model_successors.py', *args])
        return json.loads(buffer.getvalue()), exit_code

    def models_file(self):
        path = Path(tempfile.mkdtemp()) / 'models.json'
        path.write_text(MODELS_TEXT)
        return path

    def test_write_adds_the_successor_to_the_file(self):
        successors.fetch_catalog = lambda: ({
            "anthropic/claude-haiku-4.5": entry("Anthropic: Claude Haiku 4.5"),
            "anthropic/claude-haiku-5.5": entry("Anthropic: Claude Haiku 5.5"),
        }, None)
        path = self.models_file()
        result, exit_code = self.run_main("--write", str(path))
        self.assertEqual(exit_code, 0)
        self.assertEqual([s["successor"] for s in result["successors"]], ["anthropic/claude-haiku-5.5"])
        self.assertIn("anthropic/claude-haiku-5.5", successors.load_pinned_ids(path.read_text()))

    def test_without_write_leaves_the_file_alone(self):
        successors.fetch_catalog = lambda: ({
            "anthropic/claude-haiku-4.5": entry("Anthropic: Claude Haiku 4.5"),
            "anthropic/claude-haiku-5.5": entry("Anthropic: Claude Haiku 5.5"),
        }, None)
        path = self.models_file()
        self.run_main(str(path))
        self.assertEqual(path.read_text(), MODELS_TEXT)

    def test_a_catalog_failure_is_reported_not_raised(self):
        successors.fetch_catalog = lambda: ({}, "timed out")
        result, exit_code = self.run_main("--write", str(self.models_file()))
        self.assertEqual(exit_code, 0)
        self.assertIn("timed out", result["error"])
        self.assertEqual(result["successors"], [])


if __name__ == '__main__':
    unittest.main()
