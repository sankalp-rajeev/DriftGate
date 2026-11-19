# Lab Constraints and Equivalences

FR-28 appendix; all substitutions were approved by the instructor on 18 Nov 2026, and evidence IDs are listed in [lab-constraints-evidence.md](lab-constraints-evidence.md).

| Constraint | What the Lab does | Equivalent used | Evidence | What is lost | Enforced or documented |
| --- | --- | --- | --- | --- | --- |
| CodePipeline | CodePipeline cannot assume LabRole, so no pipeline can run. | An EventBridge rule invokes an event-driven Lambda orchestrator that keeps a run record in S3 (ADR-007). | E1, E2, E3 | The managed pipeline view, stage retries and execution history are rebuilt by hand. | n/a |
| CodeBuild | CodeBuild returns AccessDeniedException for LabRole. | SSM Run Command drives `deploy/build.sh` on an EC2 build host (ADR-008). | E4, E5, E6 | Builds share one persistent host instead of fresh managed containers. | n/a |
| IAM role creation | Role creation is denied, so only LabRole and LabInstanceProfile exist. | Every service runs as LabRole or LabInstanceProfile. | E7 | Every component holds all of LabRole's permissions. | Documented only |
| Least privilege | Scoped roles cannot be attached, but resource-level controls can be set. | Policy documents in `iam/` plus resource-level controls (NFR-06). | E8, E9, E10 | Identity-level least privilege is not enforced at runtime. | Enforced: ALB port 80 only, ALB-only serving ingress, no build-host inbound, no SSH, IMDSv2, the bucket policy, Lambda resource policies and 1,800 s approval tokens; documented only: the identity policies, including build-host start/stop. |
| No domain or certificate | The Lab has no domain or TLS certificate for the ALB. | The ALB listens on HTTP 80 only (PRD §6.8). | E11 | The live URL has no TLS. | Enforced |
| NAT gateway | NAT gateway creation has not been probed yet. | `UseNat=true` is the default, and `UseNat=false` puts instances in public subnets behind ALB-only security groups. | E12 | With `UseNat=false`, instances have public IPs. | Enforced |
| Research layer on laptop | Credit and 4-hour sessions cannot fit the fault matrix (ADR-004). | Fault runs, calibration, Prometheus, Grafana and Jaeger run on the laptop, and CloudWatch serves the Lab. | E13, E14 | Calibration numbers come from the laptop CPU, not t3.large. | n/a |
| Cost Explorer lag | Cost data lags about a day, and Lab credit updates every 8 to 12 hours. | A Cost Explorer snapshot before teardown is paired with the Lab spend panel. | E15, E16, E17 | The last day of spend may be missing. | n/a |
| Session stops | Sessions end after 4 hours and instances stop. | A per-session network stack (`make up`, `make down`) sits beside the persistent `driftgate-storage` bucket. | E18, E19, E20 | The ALB DNS name changes every session. | n/a |
