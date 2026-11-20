---
date: 2026-11-20
faults_sha256: 81f02e436c4842a30bbca7c7780cf5748c61a83ed6f9e1f32372ba1f67a7ff01
baseline:
  model_sha256: d7efb072e7724d25048a4fda0a3e10b04bdef5d06b1403a1c93bd9f1240a63c8
  config_hash: 5c1277b1db8a99d27b529ae10370372e4da22ad6134478f18dcd8aae19d75d49
---

# Gate request errors, 20 November 2026

Owner-authorized deviation for T-15, FR-06, FR-08, FR-09, FR-11 and FR-13. Found during
F7-1 in `runs/2026-11-19-study/`: the run started at 01:26:28 on 20 November and crashed
around 01:27 with HTTP 400 in D5's `/completion` request. F7 sets the context window to 256,
shorter than some panel prompts. The original harness propagated the HTTP exception and
discarded the in-memory measurement instead of recording detector evidence.

## Error rule

When the candidate server returns an HTTP error or a gate request times out, the affected
detector records `status: error`, the HTTP `status_code` and response message. For a timeout,
`status_code` is null, `kind` is `timeout`, and the exception message is retained. An HTTP error
uses `kind: http` and preserves the response body (or HTTP reason if the body is empty).

The error **counts as a detection** for that detector, for gating, and in the fault matrix.
It blocks the candidate even in report mode or without a threshold for that detector.
The finite existing failure-score sentinel is used; no numeric detector measurement is invented.

Raw measurements preserve successful items and append the failed item's id and `error` object.
The detector stops at its first request error, and other detectors continue. At a smaller budget,
an error counts only if that budget includes the failed item. D6 inherits errors in the D3
requests from which it obtains timings. A failed `/props` preflight affects all live detectors.
A reference containing request errors cannot be written or used as a baseline. Calibration
refuses erroneous calibration samples; held-out request errors count as false alarms.

Unexpected program, build or data failures remain failed runs, with tracebacks, and do not
become detector measurements or matrix misses. The study logs every start, failure and finish,
retains failed-attempt history, and proceeds to later work. A later resume retries missing or
invalid runs while skipping complete valid runs, including completed error detections. Analysis
waits for all required runs to be complete.

**No threshold changed.** Threshold values and the calibration rule, budgets, seeds, repetition
rule, fault definitions, serving baseline and reference artifacts are unchanged. The front
matter carries forward the pins from the 19 November thread-count deviation so the runner
can validate the latest dated deviation. The frozen `docs/prereg/` files remain unchanged.

## Investigation before any rerun

The current filesystem contains the following valid completed results:

| Fault | Valid repetitions | Last finish, local time |
| --- | --- | --- |
| F2 | 2 | 00:45:02 |
| F3 | 2 | 00:56:30 |
| F4 | 2 | 01:05:53 |
| F5 | 2 | 01:16:03 |
| F6 | 2 | 01:26:28 |

All ten records have seed 0, run seeds 1 and 2, 64 D3 prompts and 256 D5 items, matching config
and detector-data hashes, and matching stored verdicts. D1 sidecars exist for every run; D2
sidecars exist for F2 and F3 and use 2 threads. The measurements can be evaluated at all three
budget levels. F1's two runs and all 30 A/A runs are also complete.

Before changing code, `make study-status` reported two runs each for F1 through F6. The actual
`study.log` contains their start and finish events and ends at `start F7-1` at 01:26:28. The
reported earlier view with zero F2–F6 runs and a 00:37:59 final log entry is not reproducible
from these files; its cause is unconfirmed. No data was missing or recovered from another run.

F7-1 has a traceback in `logs/F7-1.log` and no persisted measurement or run record. F7-2, F8
and F9 have no results. The resume point is therefore F7-1; the two repetitions of F1 through
F6 are retained, and no collected A/A or fault result is modified by this fix. The owner starts
the resumed study after merge; this fix does not run or rerun the study.
