"""Every `run:` block in every workflow must be parseable shell.

A workflow's shell is never parsed until the step executes, and a step that dies on a
syntax error looks exactly like a step that ran: `continue-on-error` reports success, and
a failure with no product-visible symptom (telemetry that stops arriving) has nothing to
alert on. The cost of that gap was the Datadog spans payload, where an apostrophe inside
a single-quoted jq program closed the string and handed the rest of the payload to bash,
silently, on every review in every consuming repo.

`bash -n` parses without executing, which is the whole check: it catches the unbalanced
quote, the unclosed heredoc and the stray `fi` at the time the workflow is edited rather
than the next time it runs somewhere else.
"""

import pathlib
import re
import subprocess
import unittest

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"

# GitHub substitutes `${{ ... }}` before the shell ever sees it. Left in place it is not
# valid bash, so stand each one up as a bare word — the same shape the runner produces
# for the common case of an expression sitting inside a quoted string.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)

# Steps declaring another interpreter are not bash and are not ours to parse.
BASH_SHELLS = {None, "bash", "sh", "bash -e {0}"}


def shell_syntax_error(script):
    """The parse error in a `run:` block, or None when it parses. Nothing is executed."""
    result = subprocess.run(
        ["bash", "-n"],
        input=EXPRESSION.sub("EXPR", script),
        capture_output=True,
        text=True,
    )
    return None if result.returncode == 0 else result.stderr.strip()


def run_blocks():
    """Every (workflow, job, step name, script) a runner would hand to bash."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in (workflow.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                if "run" not in step or step.get("shell") not in BASH_SHELLS:
                    continue
                label = step.get("name") or f"step {index}"
                yield path.name, job_name, label, step["run"]


class WorkflowShellParses(unittest.TestCase):
    def test_every_run_block_is_valid_bash(self):
        blocks = list(run_blocks())
        self.assertTrue(blocks, f"no run blocks found under {WORKFLOWS}")
        for workflow, job, step, script in blocks:
            with self.subTest(workflow=workflow, job=job, step=step):
                error = shell_syntax_error(script)
                self.assertIsNone(error, f"{workflow} / {job} / {step}:\n{error}")


class ShellSyntaxError(unittest.TestCase):
    def test_catches_an_apostrophe_closing_a_single_quoted_program(self):
        # The defect this check exists for: the apostrophe in "pi's" ends the jq program,
        # and bash parses what follows as code.
        script = (
            "spans=$(jq -n --argjson fields \"$fields\" '[\n"
            "  {\n"
            "    # alongside pi's own token counters\n"
            "    metrics: $fields.metrics\n"
            "  }\n"
            "]')\n"
        )
        self.assertIsNotNone(shell_syntax_error(script))

    def test_accepts_the_same_program_without_the_apostrophe(self):
        script = (
            "spans=$(jq -n --argjson fields \"$fields\" '[\n"
            "  {\n"
            "    # alongside the token counters pi itself records\n"
            "    metrics: $fields.metrics\n"
            "  }\n"
            "]')\n"
        )
        self.assertIsNone(shell_syntax_error(script))

    def test_a_github_expression_is_not_a_syntax_error(self):
        self.assertIsNone(shell_syntax_error('[ "${{ steps.review.outcome }}" = ok ]'))


if __name__ == "__main__":
    unittest.main()
