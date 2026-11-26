# Learner Lab session 3, 25 November 2026

| Check | Observed result |
| --- | --- |
| First end-to-end push | A clean CodeCommit push ran through Build and SNS approval to successful CodeDeploy deployment `d-U8TDIHU0N`. |
| F1 build gate | The Q8_0 to Q4_K_M model change passed the tests, SHA-256 manifest check, D1 and reference check. D2 failed and blocked the run during Build, before Approval or Deploy. |
| CloudWatch alarm drill | Synthetic `GateScore` of 5 moved the alarm to `ALARM`. CodeDeploy stopped `d-VZZYIAX0N` in about 70 seconds and skipped the second instance. Automatic rollback deployment `d-LV834YW0N` succeeded. |

## Fixes from the session

- [#75](https://github.com/sankalp-rajeev/DriftGate/pull/75) isolated test runs from Lab deployment environment variables and files.
- [#76](https://github.com/sankalp-rajeev/DriftGate/pull/76) pinned baseline serving fixtures so candidate changes could be tested against a stable baseline.
- [#77](https://github.com/sankalp-rajeev/DriftGate/pull/77) moved the candidate suite to a dedicated GitHub Actions job, outside the Lab build test run.
- [#78](https://github.com/sankalp-rajeev/DriftGate/pull/78) made the serving gate end measurement early once the verdict was decided and preserved source history during session sync.
