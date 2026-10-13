# ReplayFix — two-minute demo outline

Use the saved verified run **20261013T221547Z_45f0a4** from October 13 at about
3:15 PM Pacific. This walkthrough presents the results of an actual completed
API-and-Docker run. It does not require another paid model request. The two-minute
length leaves room within the event's under-three-minute video guidance.
See the [official rules](https://nebiusglobalaihackathon.devpost.com/rules).

From the project folder, open the included saved evidence with:

```powershell
py .\replayfix_dashboard.py --runs-dir .\examples
```

Select the October 13 verified run if it is not already selected. This opens
the working dashboard in viewing mode; no API call is made. Describe it as a
saved live run during the recording.

## 0:00–0:20 — Problem

**Show:** ReplayFix's dashboard and the before/after test counts.

**Say:**

“ReplayFix is an AI repair agent that checks its proposed fixes with executable
tests. This demo starts with a webhook handler that creates duplicate orders
when the same event is delivered more than once, including when deliveries overlap.”

## 0:20–0:40 — System

**Show:** The model label and the Reproduce, Generate, Verify, and Export stages.
If the live-progress card is no longer visible, use the saved-run results below it.

**Say:**

“NVIDIA Nemotron generates the repair through Nebius Token Factory. A local
Python controller validates the returned function and runs four unchanged tests
in Docker. The dashboard shows each attempt and keeps the execution evidence.”

## 0:40–1:10 — The useful failure

**Show:** The attempts section. Expand the first attempt's test feedback and
highlight the simultaneous-delivery failure.

**Say:**

“Here is a saved live run. The original code passed one test. Nemotron's first
proposal passed three, but two overlapping deliveries still created two orders.
ReplayFix sent that failing test output back to the model for a revision.”

## 1:10–1:35 — Verified revision

**Show:** Attempt 2, the four passing tests, and the Verified status.

**Say:**

“The second proposal passed all four tests, so the controller stopped and
exported the proposed repair. This run took about eleven seconds and used
5,937 tokens across two requests. Those are the observed results for this run.”

## 1:35–1:55 — Inspectable evidence

**Show:** The Diff and Function tabs, then the report export control.

**Say:**

“The output includes a function, a diff, and a report containing the baseline,
attempt history, test output, and token usage. The tests stay fixed throughout
the repair loop, so a reviewer can see what passed and what failed.”

## 1:55–2:00 — Scope

**Show:** The final results overview.

**Say:**

“This prototype verifies one SQLite demo. Extending the test coverage and
supporting more repair tasks are the next steps.”

## Evidence and scope

The report, original `repair.diff`, and original `verified_handler.py` are
included under `examples/20261013T221547Z_45f0a4/`. The saved handler has not been
manually improved. Its missing rollback on error is documented in `REVIEW.md`.
The demonstration establishes passage of four fixed tests, not general repair
reliability or production readiness.
