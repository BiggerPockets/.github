import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/gym/steps/configure-pi.sh'


class ConfigurePi(unittest.TestCase):
    def configure(self, routing):
        """Run the step with `openrouter.py routing` stubbed to print `routing`."""
        work = Path(tempfile.mkdtemp())
        shim = work / 'bin'
        shim.mkdir()
        python = shim / 'python3'
        python.write_text(f"#!/bin/sh\nprintf '%s' '{routing}'\n")
        python.chmod(python.stat().st_mode | stat.S_IEXEC)
        env = {**os.environ, 'PATH': f'{shim}:{os.environ["PATH"]}',
               'MODEL': 'openai/gpt-5.6-luna', 'RUNNER_TEMP': str(work),
               'GITHUB_ENV': str(work / 'env')}
        subprocess.run(['bash', str(SCRIPT)], env=env, check=True, capture_output=True)
        config = json.loads((work / 'pi-config/models.json').read_text())
        return {m['id']: m.get('compat', {}).get('openRouterRouting')
                for m in config['providers']['openrouter']['models']}

    def test_every_model_denies_data_collection_when_routing_is_unavailable(self):
        routing = self.configure('')
        self.assertTrue(routing)
        for model, provider in routing.items():
            self.assertEqual(provider, {'data_collection': 'deny'}, model)

    def test_the_evaluated_model_gets_its_routing_and_the_policy(self):
        routing = self.configure('{"sort": "throughput"}')
        self.assertEqual(routing['openai/gpt-5.6-luna'],
                         {'sort': 'throughput', 'data_collection': 'deny'})
        self.assertEqual(routing['openai/gpt-5.6-sol'], {'data_collection': 'deny'})


if __name__ == '__main__':
    unittest.main()
