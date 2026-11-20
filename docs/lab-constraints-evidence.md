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
