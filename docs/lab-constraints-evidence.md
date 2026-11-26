# Lab constraints evidence

## CodeDeploy deployment with LabRole

The project owner reported the successful Learner Lab probe on 20 November 2026.
See [PRD §14.2](../PRD.md#142-probe-results) and [Lab check #2](https://github.com/sankalp-rajeev/DriftGate/issues/2).

| Check | Observed result |
| --- | --- |
| Deployment group | Created with `LabRole` as the CodeDeploy service role |
| Revision | S3-hosted `.zip` |
| Target | EC2 instance with `LabInstanceProfile` |
| Deployment | `d-IBULITHXM` |
| Final status | `Succeeded` |

This confirms that the Lab permits CodeDeploy to use `LabRole` and deploy an S3 ZIP to an
EC2 instance with `LabInstanceProfile`. ALB traffic shifting and rollback on hook failure
or CloudWatch alarm still require validation under FR-17 and FR-19.

## Lab session 1, 24 November 2026

### Storage encryption

For [#37](https://github.com/sankalp-rajeev/DriftGate/issues/37), the
`driftgate-storage` bucket explicitly denied `PutObject` without an SSE header.
The upload succeeded when retried with `--sse AES256`.

Screenshot to attach: `E-storage-sse-deny.png`.

### Network stack

For [#13](https://github.com/sankalp-rajeev/DriftGate/issues/13), both subnet
configurations and teardown worked in the Lab.

| Operation | Observed result |
| --- | --- |
| Create with `UseNat=true` | 2m 41s |
| Delete the stack | Clean deletion, including the NAT gateway |
| Create with `UseNat=false` | 3m 12s |
| Update to `UseNat=true` via `make up` | 2m 47s |
| `make session-down` | 3m 42s |

### First CodeDeploy rollout

Deployment `d-PFYHV350N` finished `Succeeded` on both t3.large serving
instances. The ALB's `/health` endpoint returned HTTP 200. Rollback and
traffic shifting remain separate checks.

Screenshots to attach: `E-alb-health.png` and `E-codedeploy-first-deploy.png`.

### Cross-instance determinism, sample 1

The reference ran on the build host and each candidate ran on a different
serving host. All three machines were t3.large. Both serving hosts had D3
agreement of 1.0 and KL of 0 against the reference. D5 accuracy was 0.609375
on all three machines, for a drop of 0.0 on both serving hosts.

| Candidate | D3 agreement | D3 KL | D5 accuracy | D5 accuracy drop | D6 p95 ITL ratio | D6 p95 TTFT ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Serving instance 1 | 1.0 | 0 | 0.609375 | 0.0 | 0.969 | 0.912 |
| Serving instance 2 | 1.0 | 0 | 0.609375 | 0.0 | 0.938 | 0.900 |

The recorded D6 ratios used the full 64-prompt reference p95 as the denominator.
The fix for session 2 uses the first 16 reference prompts for L1. See the
[reference budget deviation](deviations/2026-11-24-t3large-reference-budget.md)
for the measured timing.

## Lab session 2, 24 November 2026

The [unattended run archive](../results/2026-11-24-lab-session2/README.md)
records revision `6c6732ba1d0e11a2572ae7330224602bffe253bd`, final `rc=0`,
and teardown of all three instances. The persistent storage stack remained.

### Binary cache and regenerated reference

The build host printed `DriftGate build host READY` at 16:45:03 local time,
before the first deploy. The shared cache contained
`bin/llama.cpp/8c16babba5d4c66e28a19d86e572e4b04f522d52/al2023-x86_64-avx512.tar.gz`
(11,470,231 bytes). The owner reports serving cache hits; the saved archive
contains the cache listing and successful deploy records, not individual
AfterInstall download logs. Subsequent deploys could reuse installed binaries.

The t3.large reference measurement and D2 logits were regenerated using that
generic AVX-512 build. The [reference log](../results/2026-11-24-lab-session2/reference.log)
records a 993-second measurement, completion after 1,115 seconds including
logit generation, and upload of both artifacts to the pinned reference key.
The D2 logits were 309,965,812 bytes with SHA-256
`64dd3315513a332e4769dd0b14eeace85e833d7305b3ad770c1a6e2d6696a06f`.

### Three identical deploys, six A/A gates

| Deployment | CodeDeploy status | Serving gate runs |
| --- | --- | ---: |
| [d-FUPLUK70N](../results/2026-11-24-lab-session2/deploy-1-d-FUPLUK70N.json) | Succeeded | 2 |
| [d-ZZQ4G970N](../results/2026-11-24-lab-session2/deploy-2-d-ZZQ4G970N.json) | Succeeded | 2 |
| [d-A6O7XR80N](../results/2026-11-24-lab-session2/deploy-3-d-A6O7XR80N.json) | Succeeded | 2 |

All three deploys used the same revision on `i-094106129f457e2cf` and
`i-0f50eb6bf39419347`. The third ID contains a letter O after 6.
All six original verdicts ran in report mode and have D3 agreement **1.0**,
KL **0**, and D5 accuracy **0.609375 vs 0.609375**, with no paired mismatches.
ITL ratios were **0.906–0.964**, approximately 0.91 and 0.95 on the two hosts;
TTFT ratios were **0.988–0.998**. L1 uses the first 16 reference prompts for D6.
The ALB [health check](../results/2026-11-24-lab-session2/alb-health.txt)
returned HTTP 200. Rollback and ALB deregistration/re-registration remain
separate checks.

Mean L1 detector time was **113.48 s D3 + 508.80 s D5 = 622.27 s (10.37 min)**,
about 3.7% over NFR-01. See the [measured timing](deviations/2026-11-24-t3large-reference-budget.md#measured-session-2-results).
The [owner-authorized threshold deviation](deviations/2026-11-24-t3large-thresholds.md)
records zero behavioral thresholds, D6 1.37, the six-run evidence limit and
the required Phase 4 F9 check. It does not claim a completed 20-run Lab calibration.

## Lab session 3, 25 November 2026

The first end-to-end CodeCommit push triggered the orchestrator, completed the SSM
build and SNS approval, and rolled out successfully as CodeDeploy deployment
`d-U8TDIHU0N` (FR-15, FR-27). The F1 weights change passed the tests, manifest,
D1 and reference checks. D2 failed during Build, blocking the run before Approval
and Deploy (FR-16).

A synthetic `GateScore` of 5 moved the CloudWatch alarm to `ALARM`. CodeDeploy
stopped `d-VZZYIAX0N` in about 70 seconds, skipped the second instance and
automatically rolled back via `d-LV834YW0N`, which succeeded (FR-19, FR-20).

The session fixes isolated tests from the Lab environment (#75), pinned the baseline
serving fixture (#76), moved the candidate suite to dedicated CI (#77), and ended
serving gate measurement early when the verdict was decided (#78). See the
[session 3 results](../results/2026-11-25-lab-session3/README.md) for links.

## Lab session 4, 26 November 2026

The recording session deployed a clean push (FR-15, FR-27). D2 blocked the
F1 weights change in Build (FR-16). F9 reduced serving threads from 2 to 1
and passed ValidateService on t3.large. The hook took 11m22s versus about
10.4 min at 2 threads, and the p95 ITL ratio stayed within the D6 threshold
of 1.37. See the [F9 measured result](deviations/2026-11-24-t3large-thresholds.md#f9-measured-result-26-november-2026).

F3, the `q4_0` KV-cache change, passed Build and failed ValidateService on
the first instance of `d-DXAUXSI1N`. The gate stopped measuring early with
D3 agreement of 0.0039 and mean approximate KL of 11.34. CodeDeploy skipped
the second instance and triggered automatic rollback (FR-18, FR-19).
See the [session 4 results](../results/2026-11-26-lab-session4/README.md).
