"""Trusted runner. The controller starts this only inside Docker."""

import io
import json
from pathlib import Path
import sys
import unittest

from patch_rules import validate_function
from replayfix_concurrency_test import concurrency_tests_for, load_app


def main():
    app = load_app()
    candidate = Path(__file__).with_name("candidate.py")
    if candidate.is_file():
        code = validate_function(candidate.read_text(encoding="utf-8"))
        # No imports or general builtins are exposed to the generated function.
        # This is an additional restriction, not a replacement for Docker.
        scope = {"__builtins__": {"Exception": Exception}}
        exec(compile(code, "candidate.py", "exec"), scope)
        app.handle_webhook = scope["handle_webhook"]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(app.WebhookTests),
        loader.loadTestsFromTestCase(concurrency_tests_for(app)),
    ])
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun == 4 and not result.skipped
    report = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": passed,
        "output": output.getvalue()[-20000:],
    }
    print(json.dumps(report), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
