# IAM: least-privilege policy documents (NFR-06)

The Learner Lab denies role creation (PRD §14.2), so every principal runs as `LabRole` or `LabInstanceProfile`. Per the instructor's guidance (18 Nov), least privilege is shown by **documentation plus enforcement where possible**:

- **documented-only:** identity policies written as the role each service *would* get. They are not attached in the Lab.
- **enforced:** a control that a file in this repo applies to a real resource: security groups, SSM instead of SSH and IMDSv2 (network stack), the S3 bucket policy (storage stack), the Lambda resource policies (orchestrator template), and the one-time approval token (orchestrator code).

`controls.json` is the index. It lists every control with its service, kind, status (`enforced` or `documented-only`) and `applied_in`, the file that applies it. A control is marked `enforced` only when `applied_in` names that file; `tests/test_nfr06_iam_policies.py` checks this. The FR-28 appendix takes its enforced/documented column from this file.

| File | Service | Kind | Status |
| --- | --- | --- | --- |
| `orchestrator-lambda.json` | Lambda orchestrator | identity | documented-only |
| `approval-lambda.json` | Lambda approval handler | identity | documented-only |
| `build-host-ec2.json` | EC2 build host | identity | documented-only |
| `serving-ec2.json` | EC2 serving instances | identity | documented-only |
| `codedeploy-service.json` | CodeDeploy service role | identity | documented-only |
| `lambda-resource-orchestrator.json` | Lambda orchestrator | resource | enforced in `orchestrator/template.yaml` (one permission per rule, exact SourceArn) |
| `lambda-resource-approval-url.json` | Lambda approval handler | resource | enforced in `orchestrator/template.yaml` |
| `s3-bucket-policy.json` | S3 artifact bucket | resource | enforced in `deploy/cloudformation/storage.yaml` (`driftgate-storage` stack) |
| approval token | Lambda approval handler | application | enforced in `orchestrator/handler.py`: random, single-use, hashed, expires after 1,800 s |
| security groups, SSM-not-SSH, IMDSv2 | ALB, EC2 | network | enforced in `deploy/cloudformation/network.yaml` |

Policies use `${Name}` placeholders (listed in `controls.json`) for account id, region and resource names. Fill them in before validating or applying. `Resource: "*"` appears only on actions that AWS does not scope to a resource ARN; `tests/test_nfr06_iam_policies.py` enforces that list.

## Validation

`aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document file://<file>` (or `RESOURCE_POLICY` for the resource policies), after substituting placeholders. This calls AWS, so it runs only in a Lab session and only if Access Analyzer is allowed there (PRD §14.2 open probe).

## Design facts these policies rely on (PRD §6.8)

- **Storage:** the bucket belongs to the persistent `driftgate-storage` stack (`deploy/cloudformation/storage.yaml`). Both EC2 roles can read and populate the shared `bin/llama.cpp/` cache. Other prefixes are `models/`, `reference/`, `bundles/`, `build-logs/`, and `runs/` (run records, indexes and the `runs/active.json` lock).
- **Encrypted puts:** the bucket policy denies `PutObject` without an `x-amz-server-side-encryption` header, so every upload must send it. `orchestrator/run_record.py` sends `AES256`; build-host uploads must pass `--sse AES256`.
- **Orchestrator and approval handler:** both run the same `Orchestrator` code. The approval handler calls `decide` in-process. Approval calls CodeDeploy `CreateDeployment`. Rejection or timeout ends the run and releases its lock.
- **Approval tokens:** `secrets.token_urlsafe(32)`, stored only as a SHA-256 hash, single use, rejected after the 1,800 s timeout. A sweep every 5 minutes rejects expired approvals. GET shows a confirm page and POST decides, so mail scanners can't approve.
- **Build host:** checkout at `/opt/driftgate`. Git uses `git-remote-codecommit` with the instance profile, which needs only `codecommit:GitPull` on the repository. It is the only instance tagged `Role=build-host` (set by its launch template), and `ssm:SendCommand` is scoped by that tag.
- **Target group:** owned by the network stack (`TargetGroupName`, default `driftgate-serving`). CodeDeploy refers to it by name; `codedeploy-service.json` scopes register and deregister to it.
- **Build-host start/stop (PRD §6.8, T-33):** the orchestrator starts and stops the build host. `ec2:StartInstances` and `ec2:StopInstances` are allowed only on instances tagged `Role=build-host` (`aws:ResourceTag/Role`), plus the read-only `ec2:DescribeInstances` and `ssm:DescribeInstanceInformation` it uses to find the host and check that SSM sees it online. Documented only, like the other identity policies.
