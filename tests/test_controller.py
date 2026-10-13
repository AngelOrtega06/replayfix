"""Offline checks only: service and Docker responses are simulated here."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import replayfix
from patch_rules import PatchRejected, replace_function, validate_function


# A deliberately incorrect patch: these checks do not provide a canned repair.
CANDIDATE = '''def handle_webhook(db, event):
    db.execute("INSERT INTO orders (event_id, customer_id) VALUES (?, ?)",
               (event["event_id"], event["customer_id"]))
    db.commit()
'''
FAILED = {"tests_run": 4, "failures": 3, "errors": 0, "skipped": 0,
          "passed": False, "output": "SIMULATED_FAILURE: duplicate orders\n"}
PASSED = {"tests_run": 4, "failures": 0, "errors": 0, "skipped": 0,
          "passed": True, "output": "SIMULATED_SUCCESS for controller check only\n"}


class ControllerTests(unittest.TestCase):
    def test_rejects_code_outside_the_function_and_unsafe_constructs(self):
        bad = [
            CANDIDATE + "\nprint('extra code')\n",
            CANDIDATE.replace('    db.commit()', '    import os'),
            CANDIDATE.replace('    db.commit()', '    db.__class__'),
            CANDIDATE.replace('    db.commit()', '    print("OK")'),
            CANDIDATE.replace('    db.commit()', '    db = event'),
            CANDIDATE.replace('    db.commit()', '    db.execute("DROP TABLE orders")'),
            CANDIDATE.replace('    db.commit()', '    while True: pass'),
            "@decorator\n" + CANDIDATE,
            CANDIDATE.replace('db, event', 'db, event=evil()'),
        ]
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaises(PatchRejected):
                validate_function(candidate)

    def test_accepts_one_code_fence_and_preserves_surrounding_source(self):
        self.assertEqual(validate_function("```python\n" + CANDIDATE + "```"), CANDIDATE)
        source = 'HEADER = 1\n\ndef handle_webhook(db, event):\n    pass\n\nUNCHANGED = 2\n'
        expected = 'HEADER = 1\n\n' + CANDIDATE + '\nUNCHANGED = 2\n'
        self.assertEqual(replace_function(source, CANDIDATE), expected)

    def test_retries_with_runner_feedback_and_stops_on_success(self):
        seen = []
        outcomes = iter([FAILED, PASSED])
        def fake_request(messages):
            seen.append(copy.deepcopy(messages))
            return CANDIDATE, {"usage": {"total_tokens": 5}, "finish_reason": "stop"}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            report = {"baseline": FAILED, "attempts": []}
            success = replayfix.repair_loop(folder, report, CANDIDATE, "tests", fake_request, lambda _: next(outcomes), 3)
            self.assertTrue(success)
            self.assertEqual(len(seen), 2)
            self.assertIn("SIMULATED_FAILURE", seen[1][-1]["content"])
            self.assertTrue((folder / "verified_handler.py").exists())
            self.assertEqual(json.loads((folder / "report.json").read_text())["status"], "verified_for_four_demo_tests")

    def test_attempt_limit_never_creates_verified_output_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            report = {"baseline": FAILED, "attempts": []}
            success = replayfix.repair_loop(folder, report, CANDIDATE, "tests", lambda _: (CANDIDATE, {}), lambda _: FAILED, 2)
            self.assertFalse(success)
            self.assertEqual(len(report["attempts"]), 2)
            self.assertFalse((folder / "verified_handler.py").exists())
            self.assertFalse((folder / "repair.diff").exists())

    def test_rejected_patch_is_not_sent_to_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {"baseline": FAILED, "attempts": []}
            with patch.object(replayfix, "run_tests") as executor:
                replayfix.repair_loop(Path(tmp), report, CANDIDATE, "tests", lambda _: ("import os", {}), executor, 1)
                executor.assert_not_called()
            self.assertEqual(report["attempts"][0]["status"], "rejected")

    def test_truncated_but_valid_python_is_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            report = {"baseline": FAILED, "attempts": []}
            with patch.object(replayfix, "run_tests", return_value=PASSED) as executor:
                success = replayfix.repair_loop(
                    folder, report, CANDIDATE, "tests",
                    lambda _: (CANDIDATE, {"finish_reason": "length"}), executor, 1,
                )
                executor.assert_not_called()
            self.assertFalse(success)
            self.assertIn("incomplete", report["attempts"][0]["reason"])
            self.assertFalse((folder / "verified_handler.py").exists())

    def test_retry_discards_truncated_history_and_uses_latest_sql_feedback(self):
        seen = []
        sql_bad = CANDIDATE.replace('    db.commit()', '    db.execute("BEGIN TRANSACTION")')
        long_answer = "OVERSIZED_RESPONSE_SENTINEL " * 1000
        answers = iter([(long_answer, {"finish_reason": "length"}),
                        (sql_bad, {"finish_reason": "stop"}),
                        (CANDIDATE, {"finish_reason": "stop"})])
        def fake_request(messages):
            seen.append(copy.deepcopy(messages))
            return next(answers)
        with tempfile.TemporaryDirectory() as tmp:
            report = {"baseline": FAILED, "attempts": []}
            with patch.object(replayfix, "run_tests", return_value=PASSED) as executor:
                success = replayfix.repair_loop(
                    Path(tmp), report, CANDIDATE, "tests", fake_request, executor, 3,
                )
                executor.assert_called_once()
            self.assertTrue(success)
            self.assertNotIn("OVERSIZED_RESPONSE_SENTINEL", json.dumps(seen[1:]))
            self.assertIn("output-token limit", seen[1][-1]["content"])
            self.assertIn("BEGIN TRANSACTION", seen[2][-1]["content"])
            self.assertIn("Unsupported SQL at function line", seen[2][-1]["content"])
            self.assertEqual(seen[2][-2]["content"], sql_bad)
            self.assertEqual(report["controller_version"], replayfix.VERSION)
            self.assertEqual([a["status"] for a in report["attempts"]], ["rejected", "rejected", "passed"])

    def test_oversized_complete_response_is_not_replayed_in_next_request(self):
        seen = []
        answers = iter(["OVERSIZED_FINAL_SENTINEL " * 1000, CANDIDATE])
        def fake_request(messages):
            seen.append(copy.deepcopy(messages))
            return next(answers), {"finish_reason": "stop"}
        with tempfile.TemporaryDirectory() as tmp:
            report = {"baseline": FAILED, "attempts": []}
            replayfix.repair_loop(Path(tmp), report, CANDIDATE, "tests", fake_request, lambda _: PASSED, 2)
            self.assertNotIn("OVERSIZED_FINAL_SENTINEL", json.dumps(seen[1]))
            self.assertIn("16,000", seen[1][-1]["content"])

    def test_sql_diagnostic_identifies_rejected_statement_without_relaxing_gate(self):
        for statement in ["SELECT COUNT(*) FROM orders WHERE event_id = ?", "INSERT OR IGNORE INTO orders (event_id, customer_id) VALUES (?, ?)", "DROP TABLE orders"]:
            with self.subTest(statement=statement), self.assertRaises(PatchRejected) as caught:
                validate_function('def handle_webhook(db, event):\n    db.execute(' + repr(statement) + ')\n')
            self.assertIn(statement, str(caught.exception))
            self.assertIn("function line 2", str(caught.exception))

    def test_runner_rejects_forged_or_incomplete_success(self):
        invalid = [dict(PASSED, tests_run=0), dict(PASSED, errors=1), dict(PASSED, skipped=1)]
        for result in invalid:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                run = subprocess.CompletedProcess([], 0, json.dumps(result), "")
                cleanup = subprocess.CompletedProcess([], 0, "", "")
                with patch.object(replayfix, "docker_call", side_effect=[run, cleanup]):
                    with self.assertRaises(replayfix.ReplayFixError):
                        replayfix.run_tests(CANDIDATE, "sha256:test", Path(tmp))

    def test_timeout_still_removes_the_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(replayfix, "docker_call", side_effect=[replayfix.ReplayFixError("timeout"), None]) as docker:
                with self.assertRaises(replayfix.ReplayFixError):
                    replayfix.run_tests(CANDIDATE, "sha256:test", Path(tmp))
            run_args = docker.call_args_list[0].args[0]
            cleanup_args = docker.call_args_list[1].args[0]
            self.assertEqual(cleanup_args, ["rm", "--force", run_args[run_args.index("--name") + 1]])
            self.assertIn("none", run_args)
            self.assertIn("--read-only", run_args)
            self.assertNotIn("--privileged", run_args)
            self.assertNotIn("NEBIUS_API_KEY", " ".join(run_args))

    def test_api_request_shape_and_response_redaction(self):
        key = "fake-test-secret-never-a-real-key"
        payload = {"choices": [{"message": {"content": CANDIDATE + "# " + key,
                                           "reasoning_content": "REASONING_SENTINEL"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        with patch.object(replayfix.urllib.request, "build_opener") as factory:
            opener = factory.return_value
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            answer, metadata = replayfix.ask_nebius(key, replayfix.DEFAULT_MODEL, [{"role": "user", "content": "repair"}])
            request = opener.open.call_args.args[0]
            sent = json.loads(request.data)
            self.assertEqual(request.full_url, replayfix.ENDPOINT)
            self.assertEqual(sent["max_tokens"], 4096)
            self.assertEqual(sent["temperature"], 0.2)
            self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
            self.assertNotIn("extra_body", sent)
            self.assertNotIn(key, request.data.decode())
            self.assertEqual(request.headers["Authorization"], "Bearer " + key)
            self.assertNotIn(key, answer)
            self.assertIn("[REDACTED]", answer)
            self.assertEqual(metadata["usage"]["total_tokens"], 30)
            self.assertNotIn("REASONING_SENTINEL", answer + json.dumps(metadata))
            self.assertEqual(metadata["generation_settings"]["chat_template_kwargs"], {"enable_thinking": False})
            replayfix.ask_nebius(key, "nvidia/test-model", [])
            self.assertNotIn("chat_template_kwargs", json.loads(opener.open.call_args.args[0].data))

    def test_api_error_is_not_retried_or_printed_raw(self):
        with patch.object(replayfix.urllib.request, "build_opener") as factory:
            opener = factory.return_value
            opener.open.side_effect = urllib.error.HTTPError(replayfix.ENDPOINT, 401, "sensitive raw body", {}, None)
            with self.assertRaises(replayfix.ReplayFixError) as caught:
                replayfix.ask_nebius("fake-secret", "nvidia/test-model", [])
            self.assertEqual(opener.open.call_count, 1)
            self.assertIn("401", str(caught.exception))
            self.assertNotIn("sensitive", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
