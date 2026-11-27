# DriftGate: next steps (post-demo)

This file is limited to work that adds value to the resume and runs in the AWS Academy Learner Lab. Budget: $30 of Lab credit.

| # | Work | Resume value | Lab status |
|---|---|---|---|
| 1 | D7 semantic-drift detector (embeddings) | High: extends the core contribution with a measured result | No new AWS service needed |
| 2 | Authenticated inference API (OAuth 2.0 / JWT, HTTPS) | Moderate: closes the auth gap (3 postings) | Probed 26 Nov: Cognito, Secrets Manager and ACM import pass; CloudFront blocked |
| 3 | Resume and README wording for what already exists | High, no code | Not applicable |

**Dropped:**
- **CloudFront:** blocked in the Lab (26 Nov); HTTPS is done on the ALB instead.
- **Managed-model gate:** Bedrock is blocked in the Lab (`ListFoundationModels` and `InvokeModel` both return AccessDenied, 26 Nov). SageMaker doesn't match what the postings ask for.
- **SQS run queue:** no resume value.

---

## Rule zero: don't touch the submitted demo

1. Tag the submitted state before any new commit:
   ```bash
   git tag -a ece528-final -m "Version presented for ECE 528, Dec 2 2026"
   git push origin ece528-final
   ```
2. Work on the branch `feat/post-demo`, pushed to GitHub only.
3. **Do not push to the CodeCommit `main` branch until the course is graded.** A push to `main` fires EventBridge and runs the real pipeline.
4. Tear down the demo resources within 24 h of the demo. After that, every Lab session starts with `make up` and ends with `make down`.
5. Find the Lab end date on the course page. It's the deadline for everything below.
6. Record the 26 Nov probe results in `docs/lab-constraints.md`, in the same format as the Nov 17 probes:

   | Probe (26 Nov, CloudShell as voclabs) | Result |
   |---|---|
   | Bedrock `ListFoundationModels`, `InvokeModel` (Titan Embed v2) | AccessDenied |
   | Cognito user pool, domain, resource server, `client_credentials` token | Pass (RS256 access token issued) |
   | Secrets Manager create, read, delete | Pass |
   | CloudFront `CreateDistribution` | AccessDenied |
   | ACM `ImportCertificate` (self-signed), delete | Pass |
   | Lab terminal AWS CLI | Too old for `bedrock` commands; probes were run in CloudShell |

---

## 1. D7 semantic-drift detector

**Why it matters:** D2 and D3 need token logprobs, so they only work on a self-hosted server. D7 compares what the answers mean, so the gate also works against endpoints that don't expose logits. It's also the best remaining chance to catch **F8 (engine bump)**, which every current detector missed.

**Design**
- **Input:** reuse the greedy continuations D3 already generates (same prompts, same seed), so D7 adds no generation time to ValidateService. The current L1 budget is already 10.4 min per instance.
- **Embedding model:** a small open embedding model in GGUF (for example `bge-small-en-v1.5`, MIT licence), served by a second `llama-server --embeddings` process on the instance during validation.
  - It uses the same pinned llama.cpp binary, so there's no new runtime.
  - Pin the file and its SHA-256 in `serving/models.yaml` and add it to `ATTRIBUTION.md`.
- **Reference:** embed the reference outputs once and store the vectors in the reference bundle in S3, next to the reference logits.
- **Statistic:** per prompt, the cosine similarity between candidate and reference embeddings. Report the mean and the 5th percentile. The gate input is `1 − p5 similarity`.
- **Thresholds:** calibrate on the A/A runs the same way as D2, D3 and D5. Write a pre-registration note in `docs/prereg/` *before* the fault runs. Thresholds go in `gate/thresholds.json` under a new key.
- **GateScore:** D7 joins the score with its own weight, and the CloudWatch alarm path doesn't change.

**Acceptance**
- `gate/d7.py` with unit tests: identical texts give similarity 1, and the timeout and empty-output paths are handled.
- A/A false-alarm rate measured on the Lab t3.large (one session, launched early so it finishes well inside the 4-hour limit), at most what the other detectors allow.
- The F1–F9 fault matrix re-run with D7 added, on the Ubuntu machine as before, because it outlives a 4-hour Lab session. Update the fault-by-detector table in `docs/results.md` and report misses honestly, F8 included. Long runs are launched in the background; the agent records the run IDs and stops.
- One end-to-end Lab run (push → gate with D7 → approve → CodeDeploy) passes, with D7 visible in the run record and GateScore.
- The ValidateService time increase is measured and reported. Target: under 1 min per instance.

**Resume bullet (fill in the numbers after the runs; claim only what the results show):**
> Added an embedding-based semantic-drift detector to the release gate; across 9 injected serving faults it caught [N], including [fault], with [x]/[y] false alarms on held-out A/A runs.

Say "embeddings", not "vector database". This work doesn't use one.

---

## 2. Authenticated inference API

**Today:** the ALB forwards port 80 straight to llama-server on 8080. Anyone with the ALB address can use the model.

### Probe results (26 Nov)

Cognito (pool, domain, `client_credentials` token), Secrets Manager and ACM certificate import all pass. CloudFront is blocked, so HTTPS goes directly on the ALB using an imported certificate.

### Design

```
client --HTTPS :443 (cert from a private CA, imported into ACM)--> ALB
   --> gateway :8080 (verifies the Cognito JWT) --> llama-server 127.0.0.1:8081
```

- **Gateway:** a small Python proxy in `serving/`, started by the CodeDeploy hooks, that sits in front of llama-server.
  - It verifies `Authorization: Bearer <JWT>` against the user pool's JWKS: signature, `iss`, `exp`, `token_use=access` and the scope `driftgate/infer`.
  - It returns 401 for a missing or invalid token and 403 for a wrong scope.
  - It fetches the JWKS at start-up and refreshes it when it sees an unknown `kid`. A copy fetched at build time goes in the bundle as a fallback, in case the instance can't reach Cognito.
  - `/health` stays unauthenticated, since the ALB health check calls the target directly.
- **llama-server** moves to `127.0.0.1:8081`. ValidateService keeps calling it on localhost, so D3, D5, D6 and D7 don't change.
- **Tokens:** OAuth 2.0 `client_credentials` flow (machine-to-machine), resource server `driftgate`, scope `infer`. The client secret lives in Secrets Manager and is read only by the caller (the demo client script). The servers never need it.
- **TLS without a domain:**
  - `scripts/make-cert.sh` creates a private CA and a server certificate with SAN `*.us-east-1.elb.amazonaws.com`. That wildcard matches every ALB DNS name in the region, so one certificate works for every per-session ALB.
  - The script imports the certificate into ACM once and prints the ARN, which `network.yaml` takes as a parameter.
  - Clients verify properly with `--cacert driftgate-ca.pem`, never `-k`.
  - The CA private key and the server key stay out of the repo (gitignored).
- **ALB:** a 443 listener with the ACM certificate, and port 80 redirects to 443. The security group allows 443 (plus 80 for the redirect).
- **CloudFormation:** the Cognito pool, domain, resource server and client go in `storage.yaml` (persistent). The 443 listener and SG change go in `network.yaml` (per session). Everything stays on LabRole; nothing creates IAM roles.
- **Optional, only if time allows:** put the approval link behind Cognito sign-in and accept approvals only from users in an `approvers` group.

### Acceptance
- No token → 401, valid token → completion, expired token → 401, wrong scope → 403.
- `curl --cacert driftgate-ca.pem https://<alb-dns>/health` succeeds with no certificate warning, and plain HTTP redirects to HTTPS.
- A full push → gate → approve → CodeDeploy run still passes, and an alarm rollback still works.
- Unit tests for the token checks use locally generated keys and never call AWS.
- The security section and architecture diagram are updated.

**Resume bullet:**
> Secured the inference API with OAuth 2.0 client-credentials tokens (Amazon Cognito, JWT verification at a gateway) and TLS on the load balancer using an ACM-imported certificate from a private CA.

---

## 3. Wording for what DriftGate already does (no code)

Make sure the resume and README say these plainly. Each one already matches lines in the postings:

- **AWS:** CodeDeploy rolling deploys, Lambda, EventBridge, Systems Manager, CloudWatch alarms, VPC, CloudFormation, S3.
- **Quantization and KV-cache effects:** the fault study measured weight quantization (Q8_0 → Q4_K_M) and KV-cache quantization (f16 → q4_0); the latter cut accuracy by 57.8 points while health checks passed.
- **LLM observability:** Prometheus, Grafana and OTel, plus the CloudWatch GateScore metric and alarm.
- **Self-hosted LLM serving:** llama.cpp serving Qwen2.5 GGUF behind an ALB.
- **Production operations:** alarm-driven automatic rollback, runbook, per-session infrastructure as code.
- **AI-assisted development:** built with Claude Code from a PRD.

---

## Working rules

- One section per Claude Code session, on `feat/post-demo`. The design here is the spec; the coding agent implements it and doesn't redo the design.
- Long runs are launched in the background and the agent stops; it doesn't monitor them.
- Every Lab session ends with `make down`. Check Cost Explorer a day after first using any new service.
- When a section is done: tests pass, docs are updated, and the status below is changed.

| Section | Status |
|---|---|
| Rule zero | Steps 1–2 and 6 complete. GitHub tag `ece528-final` points to `c458611`. Work is on `feat/post-demo`. The 26 Nov probes are recorded in `docs/lab-constraints.md`. |
| 1. D7 detector | Implemented and pre-registered in `0f51b1f`. Lint passed and 536 tests passed. Ubuntu matrix launched as `2026-11-26-d7-matrix`. Lab A/A awaits a fresh session. The calibrated threshold, Lab reference upload, ValidateService timing, end-to-end deployment, and matrix results are pending. |
| 2. Auth: probe | Done (26 Nov) |
| 2. Auth: build | To do |
| 3. Wording | To do |
