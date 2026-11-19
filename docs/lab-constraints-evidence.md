# Lab Constraints and Equivalences: evidence

Evidence for the IDs in [lab-constraints.md](lab-constraints.md) (FR-28). "To attach" items exist from the 17 Nov probes and must be exported; "to capture" items are taken during Lab sessions or the demo.

| ID | Evidence | Source | Status |
| --- | --- | --- | --- |
| E1 | CodePipeline error when assuming LabRole | Probe screenshot, 17 Nov | To attach |
| E2 | CodeCommit push to EventBridge to Lambda | Probe log, 17 Nov | To attach |
| E3 | Run record from a demo run | S3 `runs/<run_id>/` | To capture |
| E4 | CodeBuild AccessDeniedException | Probe screenshot, 17 Nov | To attach |
| E5 | SSM Run Command `AWS-RunShellScript` on an EC2 instance | Probe log, 17 Nov | To attach |
| E6 | Build log from a demo run | S3 `build-logs/` | To capture |
| E7 | IAM role creation denied | Probe screenshot, 17 Nov | To attach |
| E8 | Policy documents and enforced or documented status | `iam/*.json`, `iam/controls.json` | In repo |
| E9 | IAM Access Analyzer `validate-policy` output, if the Lab allows it | Lab session (probe open) | To capture |
| E10 | Security groups, launch templates (IMDSv2) and bucket policy | Console screenshots of the live stacks | To capture |
| E11 | HTTP 80 listener and security group; `curl http://<ALB DNS>/health` output | `deploy/cloudformation/network.yaml`; demo | To capture |
| E12 | NAT gateway probe result and `make up` with the chosen `UseNat` | Lab session | To run and capture |
| E13 | Local llama-server smoke test, about 90 to 100 tokens/s (Qwen2.5-0.5B Q4_K_M) | Log, 17 Nov | To attach |
| E14 | Grafana dashboard and Jaeger trace of one gate run (FR-23, FR-24) | Screenshots from the laptop | To capture |
| E15 | Cost Explorer per-service data about a day behind | Probe screenshot, 17 Nov | To attach |
| E16 | Cost Explorer snapshot before teardown (T-17) | Screenshot, 2 Dec | To capture |
| E17 | Lab spend panel | Screenshot | To capture |
| E18 | End of session: what still bills | Probe log (not yet run) | To run and capture |
| E19 | `make up` and `make down` creating and deleting the stack | Console screenshots | To capture |
| E20 | Empty consoles after teardown (NFR-09) | Screenshot, 3 Dec | To capture |
