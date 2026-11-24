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
