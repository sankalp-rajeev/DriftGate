# IAM: least-privilege policy documents (NFR-06)

The Learner Lab denies role creation (PRD §14.2), so every principal runs as `LabRole` or `LabInstanceProfile`. Per the instructor's guidance (18 Nov), least privilege is shown by **documentation plus enforcement where possible**:

- **documented-only:** identity policies written as the role each service *would* get. They are not attached in the Lab.
- **enforced:** controls on resources that the Lab lets us set: security groups, SSM instead of SSH, IMDSv2, the S3 bucket policy and Lambda resource policies.

`controls.json` is the index. It lists every control with its service, kind, status (`enforced` or `documented-only`) and `applied_in`, the file that applies it. `applied_in: null` on an enforced control means the task that creates that resource still has to apply the policy. The FR-28 appendix takes its enforced/documented column from this file.

| File | Service | Kind | Status |
| --- | --- | --- | --- |
| `orchestrator-lambda.json` | Lambda orchestrator | identity | documented-only |
| `approval-lambda.json` | Lambda approval handler | identity | documented-only |
| `build-host-ec2.json` | EC2 build host | identity | documented-only |
| `serving-ec2.json` | EC2 serving instances | identity | documented-only |
| `codedeploy-service.json` | CodeDeploy service role | identity | documented-only |
| `lambda-resource-orchestrator.json` | Lambda orchestrator | resource | enforced |
| `lambda-resource-approval-url.json` | Lambda approval handler | resource | enforced |
| `s3-bucket-policy.json` | S3 artifact bucket | resource | enforced |
| security groups, SSM-not-SSH, IMDSv2 | ALB, EC2 | network | enforced in `deploy/cloudformation/network.yaml` |

Policies use `${Name}` placeholders (listed in `controls.json`) for account id, region and resource names. Fill them in before validating or applying. `Resource: "*"` appears only on actions that AWS does not scope to a resource ARN; `tests/test_nfr06_iam_policies.py` enforces that list.

## Validation

`aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document file://<file>` (or `RESOURCE_POLICY` for the resource policies), after substituting placeholders. This calls AWS, so it runs only in a Lab session and only if Access Analyzer is allowed there (PRD §14.2 open probe).

## Assumptions to check against the pipeline code

- The approval handler records its decision in the run record and invokes the orchestrator to move on (FR-15, FR-27). If T-26 hands control back another way, change `approval-lambda.json`.
- The build host is the only instance tagged `Role=build-host` (set by its launch template). `ssm:SendCommand` is scoped by that tag.
- S3 prefixes: `models/`, `reference/`, `bundles/`, `build-logs/`, `runs/`.
- The bucket policy denies `PutObject` without an `x-amz-server-side-encryption` header, so uploads must send `--sse` (or the SDK equivalent).
- Not covered yet: starting and stopping the build host between runs (PRD §15 assumes it is stopped) and any CodeCommit read by the orchestrator. Add them if T-25 or T-28 need them.
