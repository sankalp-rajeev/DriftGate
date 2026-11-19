CloudFormation stacks (FR-21, ADR-005, PRD §6.8).

## `network.yaml`: per-session network stack (`make up` / `make down`)

Created at the start of a Lab session and deleted at the end, in us-east-1:

- a VPC with two public subnets (ALB) across two AZs;
- `UseNat=true` (default): two private subnets for the serving instances and the build host, behind one NAT gateway. `UseNat=false` (PRD §14.4 fallback): no NAT and no private subnets; instances run in the public subnets with public IPs, and inbound traffic is still ALB-only;
- a free S3 gateway endpoint on the route tables the instances use;
- an internet-facing ALB with an **HTTP 80** listener only (the Lab has no domain or certificate) that forwards to the serving target group;
- the **serving target group**, owned by this stack, named by `TargetGroupName` (default `driftgate-serving`), on `ServingPort` with health check `/health`. CodeDeploy (T-12) refers to it by that name; it is also exported as `${StackName}-TargetGroupName`;
- security groups (NFR-04, NFR-06): the ALB accepts TCP 80 only; serving instances accept only the ALB security group on `ServingPort`; the build host has no inbound rules; no SSH anywhere;
- launch templates for serving and the build host: Amazon Linux 2023 from the SSM parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64`, `LabInstanceProfile`, IMDSv2 required, no key pair, encrypted gp3 root volume. User data is left to T-12 and T-28.

The S3 bucket is not part of this stack. It lives in the persistent `driftgate-storage` stack, so `make down` never deletes stored data.

Instances go in the subnets from the `InstanceSubnetIds` output. Parameters are in `network-params.json`. Every taggable resource is tagged `Project=driftgate` for Cost Explorer.

```
make up     # aws cloudformation deploy ... (Lab session only)
make down   # aws cloudformation delete-stack ... && wait stack-delete-complete
```

`make lint` runs `cfn-lint` on every template here. `tests/test_fr21_network_stack.py` checks the security properties offline for both values of `UseNat`.
