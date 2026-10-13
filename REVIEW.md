# Review of the saved Nemotron patch

Review date: October 13, 2026.

Evidence: `examples/20261013T221547Z_45f0a4/`.

## Findings

The uploaded diff exactly matches replacement of `handle_webhook` with the
uploaded handler after normal newline normalization. It changes no tests or
schema. Both fixture SHA-256 values match the saved live report. The function
passes the same narrow Python and SQL validation used by ReplayFix.

The handler begins an immediate SQLite transaction before checking for the
event ID. Writers through this handler therefore serialize the read/insert
sequence. If the event is absent it inserts an order; otherwise it skips the
insert. The final COMMIT persists the write and releases the transaction.

The user's live Docker report records four passing tests on the second
proposal, after a first proposal failed the concurrency case. Its final
status is specifically `verified_for_four_demo_tests`.

## Known gap: rollback after an error

The saved function has no exception handler or rollback. For example, after
BEGIN IMMEDIATE, a missing `event_id` raises KeyError before COMMIT. If the
connection remains open, the transaction and write lock can remain open too.

A local review reproduced that SQL sequence with two ordinary SQLite
connections: the second connection received `database is locked`, and an
explicit rollback on the first connection released the lock. This review
probe executed a manually written SQL sequence; it did not run model-generated
Python on the host or call Nebius.

The four existing demo tests do not cover this error path. A next regression
test should inject a failure after the transaction begins and verify both that
the original error is propagated and that another connection can write.
Supporting this would require updating the fixed test suite and its expected
counts throughout the controller and dashboard before obtaining a new repair.

The captured handler and report remain unchanged. They are evidence for this
run, not a promise that every runtime error is handled. In particular, this
prototype assumes the handler owns its transaction and starts outside an
existing transaction. It does not provide a database uniqueness constraint or
protect writes performed by unrelated application code.

## Validation boundaries

- The four regression/concurrency results came from the user's live Nebius and
  Docker run. The authoring environment did not rerun that model request.
- The controller has 13 offline checks with simulated service/runner results.
  Those checks are separate from the four application tests.
- Package checks compare the saved diff and fixture hashes and load the evidence
  through the real dashboard parser. They do not create a synthetic live result.
- The controller never reads the sample handler as a fallback for a new repair.

The recorded raw response has 334 characters; the exported handler has 335
because validation strips outer whitespace and writes a final newline. This
normalization is part of the export path, not a manual change to the repair.
