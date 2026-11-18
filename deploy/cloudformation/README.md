Per-session CloudFormation network stack (FR-21, ADR-005).

`network.yaml` creates, in us-east-1:

- a VPC with two public subnets (ALB, NAT) and two private subnets (serving instances, build host) across two AZs;
- one NAT gateway for both private subnets, plus a free S3 gateway endpoint;
- an internet-facing ALB with an HTTP listener whose default action returns 503 until the CodeDeploy task (T-12) attaches the serving target group;
- security groups (NFR-04, NFR-06): ALB ingress 80/443 only; serving instances accept only the ALB security group on `ServingPort`; build host has no inbound rules; no SSH anywhere;
- launch templates for serving and the build host: `LabInstanceProfile`, IMDSv2 required, no key pair, encrypted gp3 root volume. The AMI and user data are left to the tasks that launch instances (T-12, T-28).

Parameters live in `network-params.json`. Every taggable resource carries `Project=driftgate` for Cost Explorer.

```
make up     # aws cloudformation deploy ... (Lab session only)
make down   # aws cloudformation delete-stack ... && wait stack-delete-complete
```

`make lint` runs `cfn-lint` on every template here; `tests/test_fr21_network_stack.py` checks the security properties offline.
