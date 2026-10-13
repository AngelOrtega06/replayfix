#!/usr/bin/env python3
"""ReplayFix local dashboard. Save beside replayfix.py and run:

    py replayfix_dashboard.py

Opens http://127.0.0.1:8765 and reads the adjacent runs folder.
Add --enable-run to activate the Run repair button. Your API key is entered
once in the terminal and held in server memory; it is never sent to the page.
Only clicking Run repair starts the existing Docker/Nebius repair pipeline.
Use --port 8766 if the default port is busy. Ctrl+C stops the server; an active
operation is allowed to finish before cancellation. Local development only.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser


RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
TESTS = [
    ("test_duplicate_event_creates_one_order", "Repeated delivery", "The same event creates one order."),
    ("test_distinct_events_for_same_customer_create_two_orders", "Distinct events", "Two event IDs still create two orders."),
    ("test_duplicate_after_database_reopen_creates_one_order", "Database reopen", "Duplicate protection survives a restart."),
    ("test_simultaneous_duplicate_deliveries_create_one_order", "Concurrent delivery", "Two overlapping deliveries create one order."),
]
EXPORTS = {"report.json", "repair.diff", "verified_handler.py"}
LABELS = {
    "verified_for_four_demo_tests": "Verified", "not_verified": "Not verified",
    "setup_ready": "Setup check", "checking": "Checking setup",
    "repairing": "In progress", "interrupted": "Interrupted",
    "stopped_with_error": "Stopped with error",
}


def read_small(path, limit=2_000_000):
    if path.resolve().parent != path.parent.resolve():
        raise ValueError("Linked files outside the selected run are not available.")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("This saved file is too large for the viewer.")
    return data.decode("utf-8-sig")


def run_folder(root, identifier):
    if not RUN_ID.fullmatch(identifier):
        raise ValueError("Invalid run identifier.")
    folder = root / identifier
    if folder.resolve().parent != root.resolve() or not folder.is_dir():
        raise ValueError("The selected run is not available.")
    return folder


def load_report(root, identifier):
    folder = run_folder(root, identifier)
    report = json.loads(read_small(folder / "report.json"))
    if not isinstance(report, dict) or not isinstance(report.get("attempts", []), list):
        raise ValueError("The saved report has an unexpected format.")
    return folder, report


def test_result(value):
    if not isinstance(value, dict):
        return None
    keys = ("tests_run", "failures", "errors", "skipped")
    counts = [value.get(k) for k in keys]
    if any(type(n) is not int or n < 0 for n in counts) or sum(counts[1:]) > counts[0]:
        return None
    result = dict(zip(keys, counts))
    result["passed_count"] = counts[0] - sum(counts[1:])
    result["passed"] = value.get("passed") is True and counts == [4, 0, 0, 0]
    result["output"] = value.get("output", "") if isinstance(value.get("output", ""), str) else ""
    result["cases"] = {}
    for name, _, _ in TESTS:
        match = re.search(r"^" + re.escape(name) + r"\s+\([^\n]*\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped[^\n]*)\s*$", result["output"], re.M)
        if match:
            state = match.group(1)
            result["cases"][name] = {"ok": "passed", "FAIL": "failed", "ERROR": "error"}.get(state, "skipped")
    return result


def summarize(identifier, report):
    attempts = [a for a in report.get("attempts", []) if isinstance(a, dict)]
    last = attempts[-1] if attempts else {}
    final = test_result(last.get("tests"))
    verified = (report.get("status") == "verified_for_four_demo_tests"
                and last.get("status") == "passed" and final is not None and final["passed"])
    label = LABELS.get(report.get("status"), "Unknown status")
    if report.get("status") == "verified_for_four_demo_tests" and not verified:
        label = "Needs review"
    started = report.get("started_utc", "")
    try:
        started = datetime.strptime(started, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        started = None
    return {"id": identifier, "started": started, "label": label,
            "verified": verified, "attempt_count": len(attempts)}


def list_runs(root):
    runs, unreadable = [], 0
    if root.is_dir():
        for folder in sorted(root.iterdir(), key=lambda p: p.name, reverse=True)[:500]:
            if not folder.is_dir() or not RUN_ID.fullmatch(folder.name):
                continue
            try:
                _, report = load_report(root, folder.name)
                runs.append(summarize(folder.name, report))
            except (OSError, ValueError, UnicodeError):
                unreadable += 1
    return {"runs": runs, "unreadable": unreadable}


def run_details(root, identifier):
    folder, report = load_report(root, identifier)
    summary = summarize(identifier, report)
    attempts = []
    tokens, usage_count = 0, 0
    for index, value in enumerate(report.get("attempts", []), start=1):
        if not isinstance(value, dict):
            continue
        usage = value.get("usage") or {}
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(total) is int and total >= 0:
            tokens += total
            usage_count += 1
        reason = value.get("reason", "")
        attempts.append({"number": index, "status": str(value.get("status", "unknown")),
                         "reason": reason if isinstance(reason, str) else "",
                         "tests": test_result(value.get("tests")), "tokens": total if type(total) is int else None})
    files = {}
    # A failed or inconsistent report must never advertise a verified patch.
    if summary["verified"]:
        for name in ("repair.diff", "verified_handler.py"):
            try:
                files[name] = read_small(folder / name, 100_000)
            except (OSError, ValueError, UnicodeError):
                pass
    return {**summary, "model": str(report.get("model", "Not recorded")),
            "baseline": test_result(report.get("baseline")),
            "attempts": attempts, "files": files,
            "tokens": tokens if usage_count else None,
            "usage_complete": usage_count == len(attempts),
            "tests": [{"id": a, "name": b, "description": c} for a, b, c in TESTS]}


def load_backend(project):
    """Load the user's existing controller, never a generated candidate."""
    path = Path(project).resolve() / "replayfix.py"
    required = [path, path.with_name("patch_rules.py"),
                path.with_name("sandbox_runner.py"),
                path.parent / "fixtures" / "replayfix_bug_demo.py",
                path.parent / "fixtures" / "replayfix_concurrency_test.py"]
    if not all(p.is_file() for p in required):
        raise ValueError("Point --runs-dir at the runs folder inside ReplayFix_Auto. Its parent must contain replayfix.py and the original starter files.")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("replayfix_dashboard_backend", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    needed = ("check_docker", "run_tests", "repair_loop", "ask_nebius", "read_key", "save_json")
    if not all(callable(getattr(module, name, None)) for name in needed):
        raise ValueError("This controller is not compatible with the dashboard. Use the supplied ReplayFix starter.")
    return module


class RunStopped(Exception):
    pass


class RepairJobs:
    """One bounded job at a time. The browser receives only public status."""

    def __init__(self, root, backend=None, api_key="", model="", max_attempts=3):
        self.root = Path(root).resolve()
        self.backend = backend
        self._api_key = api_key
        self.model = model
        self.max_attempts = max_attempts
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.state = {"status": "idle", "active": False, "phase": "idle", "message": "Ready",
                      "run_id": None, "job_id": None, "attempt": 0, "version": 0, "events": []}
        self.started = None
        self.finished = None

    @property
    def enabled(self):
        return self.backend is not None and bool(self._api_key)

    def snapshot(self):
        with self.lock:
            value = dict(self.state)
            value["events"] = [dict(event) for event in self.state["events"]]
            value["elapsed_seconds"] = int((self.finished or time.monotonic()) - self.started) if self.started is not None else 0
            return value

    def update(self, message, **changes):
        with self.lock:
            self.state.update(changes)
            self.state["message"] = message
            self.state["version"] += 1
            elapsed = int(time.monotonic() - self.started) if self.started is not None else 0
            self.state["events"].append({"seconds": elapsed, "message": message})
            self.state["events"] = self.state["events"][-60:]

    def start(self):
        with self.lock:
            if not self.enabled:
                raise PermissionError("Start the dashboard with --enable-run and enter your key in PowerShell.")
            if self.state["active"]:
                raise RuntimeError("A repair is already running. Wait for it to finish or stop it.")
            self.stop_event.clear()
            self.started, self.finished = time.monotonic(), None
            self.state = {"status": "running", "active": True, "phase": "docker",
                          "message": "Checking Docker and the test image...", "run_id": None,
                          "job_id": uuid.uuid4().hex, "attempt": 0, "version": 1, "events": []}
            self.thread = threading.Thread(target=self.work, name="replayfix-repair", daemon=False)
            self.thread.start()
        return self.snapshot()

    def cancel(self):
        with self.lock:
            if self.state["active"] and not self.stop_event.is_set():
                self.stop_event.set()
                self.update("Stopping after the current operation finishes. No further requests will be started.", status="stopping")
        return self.snapshot()

    def check_stop(self):
        if self.stop_event.is_set():
            raise RunStopped()

    def work(self):
        core, folder, report = self.backend, None, None
        outcome, message = "error", "The repair stopped unexpectedly."
        try:
            image_id = core.check_docker()
            self.check_stop()
            source = (core.ROOT / "fixtures" / "replayfix_bug_demo.py").read_text(encoding="utf-8")
            tests = (core.ROOT / "fixtures" / "replayfix_concurrency_test.py").read_text(encoding="utf-8")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            identifier = stamp + "_" + uuid.uuid4().hex[:6]
            self.root.mkdir(parents=True, exist_ok=True)
            folder = self.root / identifier
            folder.mkdir()
            report = {"started_utc": stamp, "model": self.model, "image_id": image_id,
                      "status": "checking", "attempts": [], "started_from": "dashboard",
                      "fixture_sha256": {"replayfix_bug_demo.py": hashlib.sha256(source.encode()).hexdigest(),
                                         "replayfix_concurrency_test.py": hashlib.sha256(tests.encode()).hexdigest()}}
            core.save_json(folder / "report.json", report)
            self.update("Reproducing the bug in Docker...", phase="baseline", run_id=identifier)
            baseline = core.run_tests(None, image_id, folder)
            report["baseline"] = baseline
            core.save_json(folder / "report.json", report)
            self.check_stop()
            if tuple(baseline.get(k) for k in ("tests_run", "failures", "errors", "skipped")) != (4, 3, 0, 0):
                raise ValueError("The baseline differs from the expected 4 tests / 3 failures. No model request was sent.")
            report["status"] = "repairing"
            attempt = 0

            def request(messages):
                nonlocal attempt
                self.check_stop()
                attempt += 1
                self.update("Nemotron is generating repair " + str(attempt) + " of " + str(self.max_attempts) + "...", phase="generating", attempt=attempt)
                answer = core.ask_nebius(self._api_key, self.model, messages)
                self.check_stop()
                return answer

            def execute(function):
                self.check_stop()
                self.update("Testing repair " + str(attempt) + " against the four unchanged checks...", phase="testing", attempt=attempt)
                result = core.run_tests(function, image_id, folder)
                self.check_stop()
                return result

            success = core.repair_loop(folder, report, source, tests, request, execute, self.max_attempts)
            self.check_stop()
            outcome = "verified" if success else "not_verified"
            message = "All four demo tests passed. The patch and report are saved." if success else "No verified repair within the attempt limit. Review the feedback below."
        except RunStopped:
            outcome, message = "cancelled", "Stopped. Saved results are available below; no further requests were started."
            if report is not None:
                report["status"] = "interrupted"
        except Exception as error:
            # Do not expose a traceback or raw service object to the browser.
            detail = str(error).replace(self._api_key, "[REDACTED]") if self._api_key else str(error)
            outcome, message = "error", "Stopped: " + detail[:1500]
            if report is not None:
                report["status"] = "stopped_with_error"
        finally:
            if folder is not None and report is not None:
                try:
                    core.save_json(folder / "report.json", report)
                except OSError:
                    outcome, message = "error", "The run finished, but its report could not be saved. Check free space and folder permissions."
            with self.lock:
                self.finished = time.monotonic()
                self.update(message, status=outcome, active=False,
                            phase="complete" if outcome == "verified" else self.state["phase"])

    def close(self):
        self.cancel()
        if self.thread and self.thread.is_alive():
            print("Waiting for the current operation to finish before closing. No new model requests will start.", flush=True)
            self.thread.join()
        self._api_key = ""


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReplayFix · Repair evidence</title>
<style>
:root{--ink:#152f2b;--muted:#697a75;--line:#dce5df;--paper:#f3f6f2;--white:#fff;--green:#246b4e;--mint:#d9f0b7;--red:#ac3e32;--redbg:#fff0eb;--shadow:0 8px 30px #15382c06}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,select{font:inherit}button,a,select{ -webkit-tap-highlight-color:transparent}button,a{touch-action:manipulation}button{cursor:pointer}button:disabled{cursor:default;opacity:.45}button:focus-visible,a:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #64a87c;outline-offset:3px}a{color:inherit}header{background:#132f2b;color:#eef4eb}nav{max-width:1360px;margin:auto;padding:19px 36px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{display:flex;align-items:center;gap:11px;font-size:22px;font-weight:750;letter-spacing:-.8px}.brand svg{width:33px;height:33px}.nav-right{display:flex;align-items:center;gap:14px;font-size:12px;color:#bfd0c8}.local{border:1px solid #466057;border-radius:40px;padding:5px 11px}.local:before{content:"";display:inline-block;width:6px;height:6px;background:#c1e896;border-radius:50%;margin-right:7px}.nav-rule{height:20px;width:1px;background:#466057}
main{max-width:1360px;margin:auto;padding:38px 36px 25px}.eyebrow{font-size:11px;font-weight:750;letter-spacing:1.65px;text-transform:uppercase;color:var(--green);margin-bottom:9px}.hero{display:flex;justify-content:space-between;align-items:center;gap:30px;margin-bottom:28px}h1{font-size:clamp(25px,3vw,38px);line-height:1.17;letter-spacing:-1.2px;font-weight:650;margin:0 0 11px}.hero p{color:var(--muted);margin:0;font-size:14px}.hero-mark{width:64px;height:64px;border:1px solid #ccd9cb;border-radius:50%;display:grid;place-items:center;color:var(--green);font-size:30px;flex:none;background:#eaf0e4}.toolbar{display:flex;align-items:center;gap:13px;padding:14px 18px;background:var(--white);border:1px solid var(--line);border-radius:12px;margin-bottom:20px;box-shadow:var(--shadow)}label{font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;white-space:nowrap;color:var(--muted)}select{max-width:100%;min-width:0;flex:1;border:0;background:transparent;color:var(--ink);padding:6px;font-weight:600}.button{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--line);background:#fff;border-radius:7px;padding:8px 13px;text-decoration:none;font-size:12px;font-weight:650;white-space:nowrap}.button:hover{background:#f1f6ed}.button.primary{background:var(--ink);border-color:var(--ink);color:white}.button.primary:hover{background:#285342}.notice{padding:12px 17px;border:1px solid #e3d9ab;border-radius:8px;background:#fffbe8;color:#796027;margin:0 0 18px}.notice.error{color:var(--red);border-color:#edcfc7;background:var(--redbg)}[hidden]{display:none!important}.status-row{display:flex;justify-content:space-between;align-items:center;gap:15px;margin:22px 0 16px}.status-line{display:flex;gap:10px;align-items:center}.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:6px;font-size:11px;font-weight:750;background:#eaf1ea;color:#526356;white-space:nowrap}.badge.good{color:#225c3c;background:#e3f3d4}.badge.bad{color:#a34433;background:#ffece5}.badge.wait{color:#8b682b;background:#fff2d4}.subtle{font-size:12px;color:var(--muted)}.run-id{font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);overflow-wrap:anywhere}.stats{display:grid;grid-template-columns:1.1fr 1.1fr .8fr .9fr;gap:14px;margin-bottom:24px}.stat{border:1px solid var(--line);background:white;padding:20px 22px;border-radius:12px;min-width:0;box-shadow:var(--shadow)}.stat.before{border-top:3px solid #d89d8b}.stat.after{border-top:3px solid #80ab59}.stat-label{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);font-weight:750}.stat-value{font-size:35px;font-weight:650;letter-spacing:-1.1px;line-height:1.3;margin:7px 0 4px}.stat-value small{font-size:19px;font-weight:450;letter-spacing:-.5px;color:#91a197}.stat-foot{font-size:11px;color:var(--muted)}.before .stat-value{color:var(--red)}.after .stat-value{color:var(--green)}.workspace{display:grid;grid-template-columns:1fr 1.1fr;gap:22px;align-items:stretch}.panel{border:1px solid var(--line);background:white;border-radius:13px;overflow:hidden;min-width:0;box-shadow:var(--shadow)}.panel-head{padding:18px 21px;display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid var(--line)}h2{margin:0;font-size:15px;font-weight:700;letter-spacing:-.2px}.caption{font-size:11px;color:var(--muted);margin-top:3px}.table-wrap{padding:0 21px}table{border-collapse:collapse;width:100%}th{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);font-weight:600;padding:15px 0 9px;text-align:left}th:not(:first-child),td:not(:first-child){text-align:right}td{padding:17px 0;border-top:1px solid #edf1ed;vertical-align:middle}td:first-child{padding-right:14px}.test-name{font-size:12px;font-weight:650}.test-desc{font-size:10px;color:var(--muted);margin-top:4px;max-width:250px}.test-state{font-size:10px;display:inline-flex;gap:5px;align-items:center;color:var(--muted);font-weight:650;white-space:nowrap}.test-state.passed{color:var(--green)}.test-state.failed,.test-state.error{color:var(--red)}.test-state.skipped{color:#987533}.logs{border-top:1px solid var(--line);padding:12px 21px 15px}.logs details+details{margin-top:8px}summary{font-size:11px;color:var(--muted);cursor:pointer;user-select:none}pre{margin:0;font:11px/1.75 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;tab-size:4;overflow:auto}.log-text{white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;background:#f6f8f3;padding:12px;border-radius:7px;margin-top:9px;color:#51635c}.code-panel{background:#142722;border-color:#142722;color:#e3ece3;display:flex;flex-direction:column}.code-panel .panel-head{border-color:#34453d}.code-panel .caption{color:#96ada1}.code-panel h2{color:#edf5e8}.code-tabs{display:flex;flex-wrap:wrap;border-bottom:1px solid #34453d;padding:0 20px;align-items:center;gap:18px}.tab{padding:12px 0 10px;background:none;border:0;border-bottom:2px solid transparent;font-size:11px;color:#a0b1a7}.tab[aria-selected="true"]{border-bottom-color:#c3e998;color:#daefbd}.copy-button{margin-left:auto;border:1px solid #3a5044;background:transparent;color:#c9d9cc;border-radius:5px;padding:4px 9px;font-size:10px}#copyCode{margin-left:0}.wrap-control[aria-pressed="true"]{border-color:#88ad69;color:#daefbd}#codeView.wrapped .code-line{white-space:pre-wrap;overflow-wrap:anywhere}.code-scroll{padding:16px 0;flex:1;min-height:290px;max-height:360px;overflow:auto}.code-line{display:block;padding:0 20px;min-height:19px;white-space:pre}.code-line.add{background:#234834;color:#c6efaf}.code-line.remove{background:#492e27;color:#edbeb1}.code-line.hunk{color:#88babb}.code-line.file{color:#96a999}.code-empty{padding:32px 22px;font-size:12px;color:#9daf9f;white-space:pre-wrap;line-height:1.8}.code-bottom{padding:13px 20px;border-top:1px solid #34453d;display:flex;gap:12px;justify-content:space-between;align-items:center;color:#96ada1;font-size:10px}.code-bottom a{color:#daefbd;text-underline-offset:3px}.activity{margin-top:22px}.activity-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0}.attempt{padding:20px 22px;border-right:1px solid var(--line);min-width:0}.attempt:last-child{border-right:none}.attempt-top{display:flex;align-items:center;gap:10px;margin-bottom:10px}.attempt-num{width:25px;height:25px;border-radius:50%;display:grid;place-items:center;background:#f0f4ea;font-size:10px;font-weight:700}.attempt-title{font-size:12px;font-weight:650}.attempt p{color:var(--muted);font-size:11px;margin:0;overflow-wrap:anywhere}.attempt .badge{margin-left:auto;padding:3px 7px;font-size:10px}.model-row{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;padding:17px 0 0;color:var(--muted);font-size:11px}.model-row code{color:#4e665b;word-break:break-word;font-size:11px}.empty{background:white;border:1px solid var(--line);border-radius:14px;padding:50px 26px;text-align:center}.empty h2{font-size:22px;margin-bottom:12px}.empty p{color:var(--muted);margin:7px auto;max-width:550px}.empty code,.next code{font:12px/1.8 ui-monospace,Consolas,monospace;color:var(--ink);background:#e9efe4;border-radius:5px;padding:4px 8px}.next{margin-top:23px;border-top:1px solid var(--line);padding-top:16px;color:var(--muted);font-size:12px}.next p{margin:9px 0}.next summary{color:var(--ink);font-size:12px;font-weight:650}footer{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;border-top:1px solid var(--line);margin-top:25px;padding-top:16px;color:#7f8c84;font-size:10px;letter-spacing:.15px}.toast{position:fixed;bottom:25px;left:50%;transform:translateX(-50%);background:#15382c;color:white;padding:10px 18px;border-radius:8px;box-shadow:0 5px 20px #0002;font-size:12px;z-index:5}
@media(max-width:1000px){.workspace{grid-template-columns:1fr}.code-scroll{max-height:390px}.code-panel{min-height:400px}.test-desc{max-width:none}}@media(max-width:620px){nav{padding:16px 18px}.nav-right .providers,.nav-rule{display:none}main{padding:26px 18px 20px}.hero-mark{display:none}.hero{margin-bottom:22px}h1{font-size:29px}.hero p{font-size:12px}.toolbar{flex-wrap:wrap;padding:12px}.toolbar label{flex-basis:100%}.toolbar select{width:calc(100% - 95px)}.toolbar #reportLink{display:none}.stats{grid-template-columns:1fr 1fr;gap:10px}.stat{padding:15px}.stat-value{font-size:30px}.stat-label{font-size:9px}.stat-foot{font-size:10px}.status-row{align-items:flex-start;flex-direction:column;gap:8px}.panel-head{padding:16px}.table-wrap{padding:0 16px}.test-desc{max-width:170px;font-size:10px}td{padding:15px 0}.test-state{font-size:9px}.logs{padding:12px 16px}.activity-grid{grid-template-columns:1fr}.attempt{border-right:0;border-bottom:1px solid var(--line)}.attempt:last-child{border-bottom:0}.code-bottom{align-items:flex-start;flex-direction:column;gap:6px}.brand{font-size:20px}footer{gap:7px}.caption{font-size:10px}}
.launch-panel{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:22px 24px;background:#e9f0e2;border:1px solid #cdddc4;border-radius:12px;margin-bottom:20px}.launch-panel .eyebrow{font-size:10px;margin-bottom:4px}.launch-panel h2{font-size:18px}.launch-panel p{font-size:12px;color:#596f5e;margin:5px 0}.launch-model{font:10px/1.5 ui-monospace,Consolas,monospace;color:#6b7e6d;overflow-wrap:anywhere}.launch-actions{display:flex;gap:10px;align-items:center;flex-shrink:0}.launch-button{padding:12px 19px!important;font-size:13px!important}.live-run{padding:19px 24px 17px;background:white;border:1px solid #c8dabc;border-radius:12px;margin-bottom:20px;box-shadow:var(--shadow)}.live-head{display:flex;align-items:center;gap:13px}.elapsed{margin-left:auto;font-variant-numeric:tabular-nums}.live-title{font-size:14px;font-weight:600;margin:13px 0 17px;overflow-wrap:anywhere}.phase-list{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.phase-step{font-size:11px;border-top:3px solid #e7ede4;padding-top:9px;color:#8b988d}.phase-step span{font-size:9px;margin-right:5px}.phase-step.current{color:#285c40;border-color:#8faf6c;font-weight:700}.phase-step.done{color:#36784a;border-color:#62944b}.phase-step.halted{color:#9b5139;border-color:#cb9171}.live-details{margin-top:17px}.live-details ol{padding-left:18px;margin:10px 0 0;font:11px/1.85 ui-monospace,Consolas,monospace;color:#65796b}.live-details li{padding-left:3px;overflow-wrap:anywhere}@media(max-width:620px){.launch-panel{padding:18px;align-items:flex-start;flex-direction:column;gap:14px}.launch-actions{width:100%}.launch-button{flex:1}.launch-panel h2{font-size:16px}.live-run{padding:17px}.live-head{flex-wrap:wrap;gap:8px}.elapsed{font-size:10px}.phase-list{gap:7px}.phase-step{font-size:10px}.phase-step span{display:block;margin-bottom:3px}.launch-model{font-size:9px}}
</style>
</head>
<body>
<header><nav><div class="brand"><svg viewBox="0 0 36 36" fill="none" aria-hidden="true"><path d="M27 12a11 11 0 0 0-19 5" stroke="#c3e998" stroke-width="3" stroke-linecap="round"/><path d="m7 9 1 8 8-1M9 24a11 11 0 0 0 19-5" stroke="#c3e998" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><path d="m29 27-1-8-8 1" stroke="#c3e998" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>ReplayFix</div><div class="nav-right"><span class="providers">NVIDIA Nemotron + Nebius Token Factory</span><span class="nav-rule"></span><span class="local">Local repair workspace</span></div></nav></header>
<main>
<section class="hero"><div><div class="eyebrow">Reproduce. Repair. Verify.</div><h1>Every repair comes with evidence.</h1><p>Run a repair, watch the tests, and inspect the evidence.</p></div><div class="hero-mark" aria-hidden="true">↗</div></section>
<section class="launch-panel"><div><div class="eyebrow">Run the agent</div><h2>Fix the duplicate-order demo</h2><p id="launchHint">Checking the local session…</p><span class="launch-model" id="launchModel"></span></div><div class="launch-actions"><button class="button primary launch-button" id="startRepair" disabled>▶ Run repair</button><button class="button" id="stopRepair" hidden>Stop</button></div></section>
<section class="live-run" id="liveRun" hidden><div class="live-head"><span class="badge wait" id="liveBadge">Running</span><span class="subtle" id="liveAttempt"></span><span class="subtle elapsed" id="liveTime"></span></div><div class="live-title" id="liveTitle" role="status"></div><div class="phase-list" aria-label="Repair stages"><div class="phase-step"><span>01</span> Reproduce</div><div class="phase-step"><span>02</span> Generate</div><div class="phase-step"><span>03</span> Verify</div><div class="phase-step"><span>04</span> Export</div></div><details class="live-details"><summary>Live activity</summary><ol id="liveEvents"></ol></details></section>
<div class="toolbar"><label for="runSelect">Saved run</label><select id="runSelect" aria-label="Choose a saved run"><option>Loading saved runs…</option></select><button class="button" id="refresh">↻ Refresh</button><a class="button primary" id="reportLink" hidden>Export report ↓</a></div>
<div id="notice" class="notice" role="status" hidden></div>
<section id="empty" class="empty" hidden><div class="eyebrow">Ready when you are</div><h2>No saved runs found</h2><p>Save this dashboard beside <strong>replayfix.py</strong>, in the folder that contains your <strong>runs</strong> folder.</p><p>To create a run, open a second PowerShell window in that folder:</p><p><code>py .\replayfix.py</code></p><p>Return here and select Refresh when the repair finishes, or enable the Run repair button at startup.</p></section>
<section id="content" hidden>
<div class="status-row"><div class="status-line"><span class="badge" id="runStatus"></span><span class="subtle" id="statusNote"></span></div><span class="run-id" id="runId"></span></div>
<div class="stats"><article class="stat before"><div class="stat-label">Before repair</div><div class="stat-value" id="beforeCount">—</div><div class="stat-foot" id="beforeNote"></div></article><article class="stat after"><div class="stat-label">Latest test result</div><div class="stat-value" id="afterCount">—</div><div class="stat-foot" id="afterNote"></div></article><article class="stat"><div class="stat-label">Repair attempts</div><div class="stat-value" id="attemptCount">—</div><div class="stat-foot" id="attemptNote"></div></article><article class="stat"><div class="stat-label">Tokens recorded</div><div class="stat-value" id="tokenCount">—</div><div class="stat-foot" id="tokenNote"></div></article></div>
<div class="workspace">
<section class="panel"><div class="panel-head"><div><h2>Behavior, before and after</h2><div class="caption">The same four checks, unchanged.</div></div><span class="badge">SQLite webhook</span></div><div class="table-wrap"><table><thead><tr><th scope="col">Test case</th><th scope="col">Before</th><th scope="col" id="afterHeading">After</th></tr></thead><tbody id="testRows"></tbody></table></div><div class="logs"><details><summary>Original test output</summary><pre class="log-text" id="beforeLog"></pre></details><details><summary>Latest test output</summary><pre class="log-text" id="afterLog"></pre></details></div></section>
<section class="panel code-panel"><div class="panel-head"><div><h2>Proposed repair</h2><div class="caption">handle_webhook(db, event)</div></div><span class="badge" id="patchStatus">Saved patch</span></div><div class="code-tabs" role="tablist" aria-label="Patch view"><button class="tab" id="diffTab" role="tab" aria-selected="true" aria-controls="codeView">Changes</button><button class="tab" id="handlerTab" role="tab" aria-selected="false" aria-controls="codeView">Repaired function</button><button class="copy-button wrap-control" id="wrapCode" aria-pressed="true" title="Wrap long code lines">Wrap on</button><button class="copy-button" id="copyCode">Copy</button></div><div class="code-scroll" id="codeScroll"><pre id="codeView" role="tabpanel" aria-labelledby="diffTab" tabindex="0"></pre></div><div class="code-bottom"><span>Saved proposal · original files untouched</span><a id="patchLink" hidden>Download patch ↓</a></div></section>
</div>
<section class="panel activity"><div class="panel-head"><div><h2>Repair attempts</h2><div class="caption">Model proposals and recorded test feedback.</div></div><span class="subtle">From report.json</span></div><div class="activity-grid" id="attempts"></div></section>
<div class="model-row"><span>Model <code id="model"></code></span><span>Inference on Nebius Token Factory · tests run in Docker</span></div>
</section>
<details class="next"><summary>Run from PowerShell instead</summary><p>Keep this dashboard open. In a second PowerShell window, open your ReplayFix_Auto folder and run:</p><p><code>py .\replayfix.py</code></p><p>The repair command uses Nebius credits. Viewing these saved results makes no API calls. Select Refresh after the run finishes.</p></details>
<footer><span>CONSTRUCTED DEMO · Four passing tests verify these scenarios, not every production condition.</span><span>Local results · No API key needed to view</span></footer>
</main><div class="toast" id="toast" role="status" hidden></div>
<script>
'use strict';
const $=id=>document.getElementById(id);
let current=null,view='diff',wrapLines=true,requestSequence=0;
let session=null,jobBusy=false,lastJobRevision='',pollInFlight=false;
function text(id,value){$(id).textContent=value;}
function note(message,error=false){$('notice').textContent=message;$('notice').className='notice'+(error?' error':'');$('notice').hidden=!message;}
async function api(path){const r=await fetch(path,{cache:'no-store'});const value=await r.json();if(!r.ok)throw new Error(value.error||'Could not read this run.');return value;}
function fileURL(name){return '/download?id='+encodeURIComponent(current.id)+'&file='+encodeURIComponent(name);}
function timestamp(value){return value?new Date(value).toLocaleString([], {dateStyle:'medium',timeStyle:'short'}):'Date not recorded';}
function badge(element,label,style){element.textContent=label;element.className='badge'+(style?' '+style:'');}
function counts(id,noteId,result){$(id).replaceChildren();if(!result){text(id,'—');text(noteId,'No test result recorded');return;}$(id).append(document.createTextNode(String(result.passed_count)));const denominator=document.createElement('small');denominator.textContent=' / '+result.tests_run;$(id).append(denominator);text(noteId,result.failures+' failures · '+result.errors+' errors'+(result.skipped?' · '+result.skipped+' skipped':''));}
function stateCell(state){const td=document.createElement('td'),span=document.createElement('span');span.className='test-state '+(state||'');const states={passed:'✓ Pass',failed:'× Fail',error:'! Error',skipped:'– Skipped'};span.textContent=states[state]||'— Unlisted';td.append(span);return td;}
function renderCode(){const name=view==='diff'?'repair.diff':'verified_handler.py';const code=current?.files[name];const pre=$('codeView');pre.replaceChildren();$('diffTab').setAttribute('aria-selected',String(view==='diff'));$('handlerTab').setAttribute('aria-selected',String(view==='handler'));pre.setAttribute('aria-labelledby',view==='diff'?'diffTab':'handlerTab');$('copyCode').disabled=!code;if(!code){pre.className='code-empty';pre.textContent=current?.verified?'This file was not found in the saved run. Your test result is still available on the left.':'A verified patch will appear here after all four tests pass. See the recorded attempts below.';return;}pre.className=wrapLines?'wrapped':'';for(const line of code.split('\n')){const span=document.createElement('span');let kind='';if(view==='diff'){kind=line.startsWith('+++')||line.startsWith('---')?'file':line.startsWith('+')?'add':line.startsWith('-')?'remove':line.startsWith('@@')?'hunk':'';}span.className='code-line'+(kind?' '+kind:'');span.textContent=line||' ';pre.append(span);}}
function attemptDescription(attempt){
if(attempt.status==='rejected'){
if(attempt.reason==='Return a replacement function under 16,000 characters.')return 'The response exceeded the 16,000-character limit and was not run.';
return 'Not run. '+(attempt.reason||'The response did not meet the patch requirements.');
}
return attempt.reason||(attempt.tests?attempt.tests.passed_count+' of '+attempt.tests.tests_run+' tests passed.':attempt.status==='requesting'?'No completed response recorded.':'No completed test result recorded.');
}
function renderRun(run){current=run;$('content').hidden=false;$('empty').hidden=true;badge($('runStatus'),run.verified?'✓ Verified':run.label,run.verified?'good':run.label==='Not verified'||run.label==='Needs review'?'bad':'wait');text('statusNote',run.verified?'All four demo tests passed.':'Saved results; refresh after a new run.');text('runId',run.id);text('model',run.model);const tested=run.attempts.filter(a=>a.tests);const last=tested.length?tested[tested.length-1]:null;const final=last?.tests;counts('beforeCount','beforeNote',run.baseline);counts('afterCount','afterNote',final);text('afterHeading',last?'Attempt '+last.number:'After');text('attemptCount',run.attempt_count);text('attemptNote',run.verified?'Stopped after verification':run.attempt_count?'See each result below':'No model request recorded');text('tokenCount',run.tokens===null?'—':run.tokens.toLocaleString());text('tokenNote',run.tokens===null?'No usage recorded':run.usage_complete?'Input + output, all attempts':'Input + output, partial usage');$('testRows').replaceChildren();for(const test of run.tests){const row=document.createElement('tr'),cell=document.createElement('td'),name=document.createElement('div'),desc=document.createElement('div');name.className='test-name';name.textContent=test.name;desc.className='test-desc';desc.textContent=test.description;cell.append(name,desc);row.append(cell,stateCell(run.baseline?.cases[test.id]),stateCell(final?.cases[test.id]));$('testRows').append(row);}text('beforeLog',run.baseline?.output||'No baseline output recorded.');text('afterLog',final?.output||'No repair test output recorded.');badge($('patchStatus'),run.verified?'Tests passed':'Awaiting verification',run.verified?'good':'wait');$('reportLink').hidden=false;$('reportLink').href=fileURL('report.json');$('patchLink').hidden=!run.files['repair.diff'];$('patchLink').href=fileURL('repair.diff');$('attempts').replaceChildren();if(!run.attempts.length){const p=document.createElement('div');p.className='attempt subtle';p.textContent='No repair attempt has been recorded yet.';$('attempts').append(p);}for(const attempt of run.attempts){const div=document.createElement('article'),top=document.createElement('div'),number=document.createElement('span'),title=document.createElement('span'),status=document.createElement('span'),p=document.createElement('p');div.className='attempt';top.className='attempt-top';number.className='attempt-num';number.textContent=attempt.number;title.className='attempt-title';title.textContent='Nemotron proposal';const labels={passed:'Passed',failed:'Failed',rejected:'Output rejected',requesting:'Request sent',testing:'Testing'};badge(status,labels[attempt.status]||attempt.status,attempt.status==='passed'?'good':attempt.status==='failed'||attempt.status==='rejected'?'bad':'wait');top.append(number,title,status);p.textContent=attemptDescription(attempt);div.append(top,p);if(attempt.tests){const detail=document.createElement('details'),summary=document.createElement('summary'),output=document.createElement('pre');summary.textContent='View test feedback';output.className='log-text';output.textContent=attempt.tests.output;detail.append(summary,output);div.append(detail);}$('attempts').append(div);}renderCode();}
async function selectRun(id){const seq=++requestSequence;$('content').hidden=true;$('reportLink').hidden=true;try{const run=await api('/api/run?id='+encodeURIComponent(id));if(seq!==requestSequence)return;renderRun(run);}catch(e){if(seq===requestSequence){note(e.message,true);$('content').hidden=true;}}}
async function refresh(){const button=$('refresh');button.disabled=true;note('');try{const selected=$('runSelect').value;const data=await api('/api/runs');$('runSelect').replaceChildren();if(!data.runs.length){const option=document.createElement('option');option.textContent='No saved runs';option.value='';$('runSelect').append(option);$('content').hidden=true;$('reportLink').hidden=true;$('empty').hidden=false;current=null;}else{for(const run of data.runs){const option=document.createElement('option');option.value=run.id;option.textContent=timestamp(run.started)+' · '+run.label+' · '+run.attempt_count+' attempt'+(run.attempt_count===1?'':'s');$('runSelect').append(option);}const chosen=data.runs.find(r=>r.id===selected)||data.runs.find(r=>r.attempt_count>0)||data.runs[0];$('runSelect').value=chosen.id;await selectRun(chosen.id);}if(data.unreadable)note(data.unreadable+' report(s) could not be read. A run may still be saving; refresh to try again.');}catch(e){note('Cannot load saved runs. Keep the dashboard terminal open, then refresh. '+e.message,true);}finally{button.disabled=jobBusy;}}
$('refresh').addEventListener('click',refresh);$('runSelect').addEventListener('change',()=>{note('');selectRun($('runSelect').value);});$('diffTab').addEventListener('click',()=>{view='diff';renderCode();});$('handlerTab').addEventListener('click',()=>{view='handler';renderCode();});for(const id of ['diffTab','handlerTab'])$(id).addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const target=id==='diffTab'?'handlerTab':'diffTab';$(target).click();$(target).focus();}});$('wrapCode').addEventListener('click',()=>{wrapLines=!wrapLines;$('wrapCode').setAttribute('aria-pressed',String(wrapLines));text('wrapCode',wrapLines?'Wrap on':'Wrap off');renderCode();});$('copyCode').addEventListener('click',async()=>{const code=current?.files[view==='diff'?'repair.diff':'verified_handler.py'];if(!code)return;try{await navigator.clipboard.writeText(code);text('toast','Copied to clipboard');}catch{const range=document.createRange();range.selectNodeContents($('codeView'));const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);text('toast','Code selected. Press Ctrl+C to copy.');}$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,2400);});initialize();
function renderLive(state){
  jobBusy=state.active;
  $('startRepair').disabled=!session?.enabled||jobBusy;
  $('startRepair').textContent=jobBusy?'Repair running…':'▶ Run repair';
  $('stopRepair').hidden=!jobBusy;
  $('stopRepair').disabled=state.status==='stopping';
  $('runSelect').disabled=jobBusy;
  $('refresh').disabled=jobBusy;
  $('liveRun').hidden=state.status==='idle';
  if(state.status==='idle')return;
  const labels={running:'Running',stopping:'Stopping',verified:'Verified',not_verified:'Not verified',error:'Stopped with error',cancelled:'Stopped'};
  badge($('liveBadge'),labels[state.status]||state.status,state.status==='verified'?'good':['not_verified','error'].includes(state.status)?'bad':'wait');
  if($('liveTitle').textContent!==state.message)text('liveTitle',state.message);
  const seconds=state.elapsed_seconds;
  text('liveTime',Math.floor(seconds/60)+'m '+String(seconds%60).padStart(2,'0')+'s elapsed');
  text('liveAttempt',state.attempt?'Attempt '+state.attempt+' of '+session.max_attempts:'Preparing the demo');
  const phases={docker:0,baseline:0,generating:1,testing:2,complete:3};
  const index=phases[state.phase]??0;
  document.querySelectorAll('.phase-step').forEach((item,i)=>{
    const done=state.status==='verified'||i<index;
    item.className='phase-step'+(done?' done':i===index?' current':'')+(i===index&&!state.active&&state.status!=='verified'?' halted':'');
  });
  const revision=state.job_id+':'+state.version;
  if($('liveEvents').dataset.revision!==revision){
    $('liveEvents').dataset.revision=revision;
    $('liveEvents').replaceChildren();
    for(const event of state.events){const li=document.createElement('li');li.textContent=event.seconds+'s · '+event.message;$('liveEvents').append(li);}
  }
}
async function pollJob(){
  if(pollInFlight)return;
  pollInFlight=true;
  try{
    const state=await api('/api/job');
    renderLive(state);
    if(!state.job_id)return;
    const revision=state.job_id+':'+state.version;
    if(revision===lastJobRevision)return;
    if(state.run_id){
      let run;
      try{run=await api('/api/run?id='+encodeURIComponent(state.run_id));}
      catch(e){if(state.active)return;throw e;}
      let option=Array.from($('runSelect').options).find(o=>o.value===run.id);
      if(!option){option=document.createElement('option');option.value=run.id;$('runSelect').prepend(option);}
      option.textContent=timestamp(run.started)+' · '+run.label+' · '+run.attempt_count+' attempt'+(run.attempt_count===1?'':'s');
      $('runSelect').value=run.id;
      ++requestSequence;
      renderRun(run);
    }
    lastJobRevision=revision;
    if(!state.active)await refresh();
  }catch(e){
    note('Live status is unavailable. Keep the dashboard terminal open; it will reconnect automatically. '+e.message,true);
  }finally{pollInFlight=false;}
}
async function sendAction(path){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-ReplayFix-Token':session.token},body:'{}'});
  const result=await response.json();
  if(!response.ok)throw new Error(result.error||'The action could not be completed.');
  return result;
}
async function startRepair(){
  if(!session?.enabled||jobBusy)return;
  jobBusy=true;
  $('startRepair').disabled=true;
  $('runSelect').disabled=true;
  $('refresh').disabled=true;
  $('content').hidden=true;
  $('empty').hidden=true;
  $('reportLink').hidden=true;
  ++requestSequence;
  note('');
  try{
    const state=await sendAction('/api/start');
    lastJobRevision='';
    renderLive(state);
    await pollJob();
  }catch(e){
    note(e.message,true);
    await pollJob();
  }
}
async function stopRepair(){
  $('stopRepair').disabled=true;
  try{renderLive(await sendAction('/api/stop'));}
  catch(e){note(e.message,true);}
}
async function initialize(){
  try{
    session=await api('/api/session');
    text('launchHint',session.enabled?'Uses Nebius credits · up to '+session.max_attempts+' model requests per run.':'Viewing saved results. Restart with --enable-run to activate the repair button.');
    text('launchModel',session.enabled?session.model:'Your key is entered once in PowerShell, never in this page.');
    $('startRepair').disabled=!session.enabled;
    await refresh();
    await pollJob();
    setInterval(pollJob,1000);
  }catch(e){note('Could not connect to the local dashboard. Keep its PowerShell window open and reload this page. '+e.message,true);}
}
$('startRepair').addEventListener('click',startRepair);
$('stopRepair').addEventListener('click',stopRepair);
</script>
</body></html>'''

SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode("utf-8")).digest()).decode("ascii")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ReplayFixDashboard/2.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def send_data(self, body, content_type, status=200, filename=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'sha256-" + SCRIPT_HASH + "'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, value, status=200):
        self.send_data(json.dumps(value, ensure_ascii=False), "application/json; charset=utf-8", status)

    def local_request(self):
        port = self.server.server_port
        if self.headers.get("Host") not in {"127.0.0.1:" + str(port), "localhost:" + str(port)}:
            self.send_json({"error": "Use the localhost URL printed in your terminal."}, 403)
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_json({"error": "Cross-site requests are not available."}, 403)
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        root = self.server.runs_root
        try:
            if url.path == "/":
                self.send_data(PAGE, "text/html; charset=utf-8")
            elif url.path == "/api/session":
                jobs = self.server.jobs
                self.send_json({"enabled": jobs.enabled, "model": jobs.model,
                                "max_attempts": jobs.max_attempts,
                                "token": self.server.session_token})
            elif url.path == "/api/job":
                self.send_json(self.server.jobs.snapshot())
            elif url.path == "/api/runs":
                self.send_json(list_runs(root))
            elif url.path == "/api/run":
                self.send_json(run_details(root, query.get("id", [""])[0]))
            elif url.path == "/download":
                identifier = query.get("id", [""])[0]
                name = query.get("file", [""])[0]
                if name not in EXPORTS:
                    raise ValueError("This file is not available for export.")
                folder, report = load_report(root, identifier)
                if name != "report.json" and not summarize(identifier, report)["verified"]:
                    raise ValueError("This run has no verified repair to export.")
                self.send_data(read_small(folder / name), "text/plain; charset=utf-8", filename=identifier + "_" + name)
            else:
                self.send_json({"error": "Page not found."}, 404)
        except (OSError, UnicodeError, ValueError):
            self.send_json({"error": "This saved file is missing or unreadable. Refresh once the run has finished."}, 404)

    def do_POST(self):
        if not self.local_request():
            return
        token = self.headers.get("X-ReplayFix-Token", "")
        origin = self.headers.get("Origin", "")
        if (origin != "http://" + self.headers.get("Host", "")
                or not token.isascii() or not secrets.compare_digest(token, self.server.session_token)):
            self.send_json({"error": "Reload the local dashboard before starting a repair."}, 403)
            return
        if self.path not in {"/api/start", "/api/stop"}:
            self.send_json({"error": "Action not found."}, 404)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.send_json({"error": "Use the dashboard's Run repair button."}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 1024 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            if json.loads(self.rfile.read(length)) != {}:
                raise ValueError()
        except (ValueError, UnicodeError):
            self.send_json({"error": "Invalid action request."}, 400)
            return
        try:
            state = self.server.jobs.start() if self.path == "/api/start" else self.server.jobs.cancel()
            self.send_json(state, 202)
        except PermissionError as error:
            self.send_json({"error": str(error)}, 403)
        except RuntimeError as error:
            self.send_json({"error": str(error)}, 409)


def make_server(root, port, jobs=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.runs_root = Path(root).resolve()
    server.jobs = jobs if jobs is not None else RepairJobs(root)
    server.session_token = secrets.token_urlsafe(32)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runs-dir", type=Path, default=Path(__file__).resolve().parent / "runs")
    parser.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser.")
    parser.add_argument("--enable-run", action="store_true", help="Enable the repair button; enter your key once at the hidden terminal prompt.")
    parser.add_argument("--model", help="Optional exact Nebius model ID, as used by replayfix.py.")
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Use a port between 1 and 65535.")
    jobs = RepairJobs(args.runs_dir)
    if args.enable_run:
        try:
            backend = load_backend(args.runs_dir.resolve().parent)
            model = args.model or os.getenv("NEBIUS_MODEL", backend.DEFAULT_MODEL)
            print("Enable dashboard repairs. Your key stays in this server's memory.")
            print("Each click of Run repair may make up to " + str(args.attempts) + " paid model requests.")
            key = backend.read_key()
            jobs = RepairJobs(args.runs_dir, backend, key, model, args.attempts)
            key = ""
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled. No API request sent.")
            return 130
        except Exception as error:
            print("Could not enable repairs: " + str(error))
            return 1
    try:
        server = make_server(args.runs_dir, args.port, jobs)
    except OSError:
        jobs.close()
        print("Could not open the local port. Try: py replayfix_dashboard.py --port 8766")
        return 1
    url = "http://127.0.0.1:" + str(server.server_port)
    print("ReplayFix dashboard: " + url, flush=True)
    print("Reading saved runs from: " + str(server.runs_root), flush=True)
    print("Repairs enabled; click Run repair in the browser to begin." if jobs.enabled else "Viewing saved runs only. Add --enable-run to activate repairs.", flush=True)
    print("Keep this terminal open. Press Ctrl+C to stop.", flush=True)
    if not args.no_open:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            print("Open the URL above in your browser.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
        jobs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
