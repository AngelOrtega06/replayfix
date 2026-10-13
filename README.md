# ReplayFix

**An AI repair loop that exports code changes with test evidence.**

ReplayFix uses NVIDIA Nemotron on Nebius Token Factory to repair a deliberately
buggy SQLite webhook handler. It reproduces the bug, requests a replacement
function, validates the proposal, and runs the unchanged tests in Docker.
When a proposal fails, the next model request includes the actual test feedback.
A repair is marked verified only when all four demo tests pass.

## The problem

Webhook deliveries can repeat. In this example, the original handler creates
another order every time it receives the same event. Checking for an existing
order can solve sequential retries while still failing when two deliveries
overlap. The concurrency test is designed to catch that incomplete repair.

## A recorded live result

The saved run `20261013T221547Z_45f0a4` was started from the dashboard on
October 13, 2026, using controller version `0.3.0`.

| Stage | Result | What the tests found |
| --- | --- | --- |
| Original handler | 1 / 4 passed | Duplicate deliveries created extra orders |
| Nemotron proposal 1 | 3 / 4 passed | Concurrent delivery still created two orders |
| Nemotron proposal 2 | 4 / 4 passed | All four demo assertions passed |

The run used **2 model requests** and **5,937 total tokens**. The dashboard
displayed approximately **11 seconds** elapsed. These are observations from
one run, not a benchmark or a guarantee of future success.

The original exported report is included at
[examples/20261013T221547Z_45f0a4/report.json](examples/20261013T221547Z_45f0a4/report.json).
It records the baseline and both attempts, their test output, token usage,
generation settings, model ID, Docker image ID, and fixture hashes.
The same directory includes the original
[handler](examples/20261013T221547Z_45f0a4/verified_handler.py) and
[diff](examples/20261013T221547Z_45f0a4/repair.diff) from that run.

This result demonstrates a feedback loop: the first proposal reached the real
test runner, failed the concurrency check, and was followed by a revised proposal
that passed. Rejected or failed attempts remain visible in the evidence.

## How it works

1. Run the buggy fixture in Docker and confirm the expected baseline failures.
2. Send the fixture, tests, and baseline output to Nemotron through Token Factory.
3. Validate that the response contains only the permitted replacement function.
4. Run the four unchanged tests in an isolated Docker container.
5. Return rejection details or failing test output to the model, up to three attempts.
6. Export `verified_handler.py`, `repair.diff`, and `report.json` after verification.

| Component | Role |
| --- | --- |
| NVIDIA `nvidia/Nemotron-3_5-Lightning` | Generates and revises the repair |
| Nebius Token Factory | Hosts model inference through its chat completions API |
| Python controller | Orchestrates requests, validation, tests, and evidence |
| Docker and SQLite | Execute the fixed regression and concurrency tests |
| Local browser dashboard | Starts and stops runs, shows progress, and displays results |

Token Factory provides the runtime model endpoint, so the project does not
provision a GPU or host model weights. The controller calls
`https://api.tokenfactory.nebius.com/v1/chat/completions` and records the
provider's reported usage. Docker on the local machine executes the tests.
This version does not use Nebius AI Cloud compute or fine-tuning.

## View the included result first

From the extracted project folder, run:

```powershell
py .\replayfix_dashboard.py --runs-dir .\examples
```

On macOS or Linux, use `python3` instead of `py`. This opens the saved run in
the actual dashboard, including the patch and attempt history. This viewing
mode requires only Python and does not need Docker, an API key, or inference
credits. It displays an existing result; it does not perform a new repair.

## Run locally

The project uses Python 3.10+ and Docker with Linux containers. The host program
uses the Python standard library; no pip dependencies or local NVIDIA GPU are
required. The application source, fixtures, controller checks, and saved demo
evidence are all included in this repository.

Keep Docker Desktop running. From your project folder in PowerShell:

```powershell
docker pull python:3.12-slim
py .\replayfix.py --check
py .\replayfix_dashboard.py --enable-run
```

The setup check should run four tests with **three failures and one pass**.
Those expected failures reproduce the original bug and do not use API credits.

Enter your Nebius Token Factory API key at the hidden terminal prompt. The
`NEBIUS_API_KEY` environment variable is also supported. Open
http://127.0.0.1:8765 and click **Run repair**. Each run can make up to three
model requests; loading saved results does not call the model.

To view saved runs without enabling inference:

```powershell
py .\replayfix_dashboard.py
```

See [SETUP.md](SETUP.md) for Windows extraction instructions, CLI options,
execution restrictions, and troubleshooting.

## What is verified

The four fixed tests cover:

- Repeated delivery of the same event.
- Distinct events for the same customer.
- Duplicate delivery after reopening the database.
- A controlled overlap between deliveries using two SQLite connections.

Generated code runs only in Docker. Containers have no network access, use an
unprivileged user and a read-only filesystem, and have resource and time limits.
The narrow Python/SQL validator rejects edits to the tests or database schema.
The API key stays in the host process and is not passed to the browser or container.

This prototype supports one constructed SQLite example. Passing its four tests
does not establish general repair reliability or production correctness. It
assumes one order per event ID and a separate transaction/connection for each
delivery. It exports a proposed patch; it does not deploy it.

The saved repair uses `BEGIN IMMEDIATE` to serialize the check and insert,
but does not roll back if an exception occurs afterward. Such a failure can
leave the transaction open while its connection remains open. The four tests
do not cover this error path. See [REVIEW.md](REVIEW.md) for the review and the
next regression-test target. The original model output is preserved as evidence.

## Development checks and demo

```powershell
py -m unittest discover -s tests -v
```

Version 0.3.0 includes 13 controller checks using simulated service and runner
responses. The saved report above is separate evidence from an actual live
Nebius and Docker run on the user's machine.

[DEMO_SCRIPT.md](DEMO_SCRIPT.md) provides a two-minute walkthrough of that saved run.

## License

Project code is provided under the [MIT license](LICENSE).
