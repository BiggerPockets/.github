"""The gym replays one first-pass prompt, forever.

Recall is only comparable between runs made with the same prompt, so gym-experiment.yml
pins the prompt to GYM_PROMPT_SHA and the version it resolves to, GYM_PROMPT_VERSION.
These tests hold every job to that pin: each prompt resolution reads the pinned
checkout and is followed by the check that fails the job on any other version, and the
pinned commit really does resolve to the pinned version.
"""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gym-experiment.yml").read_text())
PIN_SHA = WORKFLOW["env"]["GYM_PROMPT_SHA"]
PIN_VERSION = WORKFLOW["env"]["GYM_PROMPT_VERSION"]


def resolve_steps():
    for name, job in WORKFLOW["jobs"].items():
        steps = job["steps"]
        for i, step in enumerate(steps):
            if "resolve-prompts.sh" in step.get("run", ""):
                yield name, steps, i, step


class GymPromptPinTest(unittest.TestCase):
    def test_pin_is_a_full_commit_sha(self):
        self.assertRegex(PIN_SHA, r"^[0-9a-f]{40}$")
        self.assertRegex(PIN_VERSION, r"^[0-9a-f]{12}$")

    def test_every_prompt_checkout_is_at_the_pin(self):
        checkouts = [step for job in WORKFLOW["jobs"].values() for step in job["steps"]
                     if (step.get("with") or {}).get("path") == "gym-prompt"]
        self.assertTrue(checkouts)
        for step in checkouts:
            self.assertEqual(step["with"]["ref"], "${{ env.GYM_PROMPT_SHA }}")

    def test_every_resolution_reads_the_pinned_checkout_and_is_checked(self):
        found = list(resolve_steps())
        self.assertEqual({name for name, *_ in found}, {"plan", "replay"})
        for name, steps, i, step in found:
            self.assertEqual(step["env"]["REGISTRY_DIR"], "gym-prompt", name)
            check = steps[i + 1]
            self.assertIn("check-prompt.sh", check["run"], name)
            self.assertIn(f"steps.{step['id']}.outputs.first_pass_prompt_version",
                          check["env"]["RESOLVED_VERSION"], name)

    def test_the_replay_reads_its_system_prompt_from_the_pinned_checkout(self):
        replay = next(step for step in WORKFLOW["jobs"]["replay"]["steps"]
                      if step.get("id") == "replay")
        self.assertEqual(replay["env"]["SYSTEM_PROMPT_FILE"],
                         "${{ github.workspace }}/gym-prompt/prompts/first-pass-system.md")
        script = (ROOT / "scripts/gym/steps/replay.sh").read_text()
        self.assertNotRegex(script, r"\$root/prompts")

    def test_the_pinned_commit_resolves_to_the_pinned_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", tmp, PIN_SHA],
                           check=True, capture_output=True)
            try:
                out = pathlib.Path(tmp) / "github-output"
                out.write_text("")
                env = dict(os.environ, GITHUB_OUTPUT=str(out), REGISTRY_DIR=tmp, PR="0")
                subprocess.run(["bash", str(ROOT / "scripts/resolve-prompts.sh")], env=env,
                               check=True, capture_output=True)
                version = re.search(r"^first_pass_prompt_version<<(\S+)\n(.*)\n\1$",
                                    out.read_text(), re.MULTILINE).group(2)
            finally:
                subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", tmp],
                               check=True, capture_output=True)
        self.assertEqual(version, PIN_VERSION)


if __name__ == "__main__":
    unittest.main()
