# Learner Lab session runbook

Run these commands in the Learner Lab with current session credentials for `us-east-1`.
Use GitHub `main` after the Phase 3+4 prep PR is merged. The Lab repository defaults to
`driftgate`; if it has a different name, set the same `REPOSITORY_NAME` for
`codecommit-push`, `cd-up` and `orch-up`.

## Start the session

1. Start Learner Lab and load its temporary AWS credentials into your shell. Install
   `git-remote-codecommit` locally if needed and make sure Git can find it on `PATH`.
   Create the CodeCommit repository in `us-east-1` on the first session and use
   `main` as its default branch.
2. Run `make storage-up` to create or update persistent storage. The build host and
   serving instances download their models from pinned Hugging Face revisions and
   check them against the manifest.
3. Before the first build host launch, run `make codecommit-push` to seed CodeCommit
   with the bootstrap script on `main`. On later sessions, leave the seeded branch
   in place until the instances are ready for a new push.
4. Run `make up`, then `make cd-up`. CloudFormation stack completion means the
   instances exist. Check `make lab-status` for their states. On the build host,
   use SSM Run Command to check `test -f /opt/driftgate/.bootstrap-done` and
   review `/var/log/driftgate-bootstrap.log`. Wait for `DriftGate build host READY`
   in that log before the first deploy: bootstrap fills the shared binary cache,
   so the serving instances hit the cache instead of compiling.
5. Run `make orch-up APPROVAL_EMAIL=you@example.org` and confirm the SNS subscription
   email when prompted. Use `make lab-status` to check all stack and instance states.

The build host and serving hooks share the llama.cpp cache in the storage bucket
at `bin/llama.cpp/<locked commit>/al2023-x86_64-avx512.tar.gz`. The first cache miss
builds both tools with `GGML_NATIVE=OFF` and `GGML_AVX512=ON`, using AVX-512
F/BW/DQ/VL shared by the t3 hosts' Intel Xeon Skylake-SP and Cascade Lake CPUs.
VNNI, VBMI and BF16 remain off. Update the cache key if the instruction flags change.
For session 2,
regenerate the reference measurement and D2 logits using that same Lab build.

Before the first pipeline smoke run, prepare the reference on the approved baseline
revision on the t3.large build host. From `/opt/driftgate`, export
`LLAMA_CPP_BIN=/opt/llama.cpp/build/bin` and use
`uv run python -m gate reference --print-key` to find the baseline's S3 key and
local directory. Generate `d2_base.kld` with `uv run python -m gate.d2 base`.

Start manual long jobs on instances with `systemd-run`. Under SSM, never use
`nohup` to detach them. With `make serve` running on the same host, start the
reference measurement as a systemd service. The t3.large default covers L1.

```sh
sudo systemd-run --unit=driftgate-reference --working-directory=/opt/driftgate \
  --setenv=LLAMA_CPP_BIN=/opt/llama.cpp/build/bin \
  /usr/local/bin/uv run python -m gate reference --seed 0
sudo journalctl -u driftgate-reference --no-pager
```

When the job completes, copy `measurement.json` and `d2_base.kld` to the S3 reference key
with `aws s3 cp --sse AES256`. In subsequent sessions, restore the pinned D2 logits to
the build host's local reference directory before a build. Bootstrap, the build's D2 step
and AfterInstall now restore the baseline key automatically if either file is missing.
They reuse the local pair when both files exist. An incomplete S3 reference fails startup
or the build before D2, so check that both objects are present before session 3.

The committed `gate/thresholds.json` enables enforce mode at L1: D2 mean KLD 0,
D3 1 minus agreement 0, D5 accuracy drop 0, and D6 p95 ITL ratio 1.37.
D2 is enforced during the build; ValidateService enforces D3, D5 and D6.
See the [session-2 threshold deviation](deviations/2026-11-24-t3large-thresholds.md)
for the six-run evidence limit and the required Phase 4 F9 check. The laptop
study's original thresholds are archived separately.

The SSM agent writes Run Command output when `OutputS3BucketName` is set. Project
code cannot set the SSE header on those uploads. Verify live in Phase 2 or 3 that
SSM writes the command logs to the storage bucket under its encryption policy.

## Session 3: pipeline and rollback evidence

1. Load the fresh Lab credentials, update the local checkout to merged GitHub `main`,
   and run `make session-up APPROVAL_EMAIL=you@example.org`. Persistent storage and
   the session-2 reference must already exist. The `codecommit` Git remote must point
   to the Lab repository with working credentials. The command checks STS, fetches
   and pushes `origin/main` to CodeCommit, runs `make up`, `make cd-up`, `make orch-up`
   in order, then waits up to 30 minutes for the build host's bootstrap marker via
   SSM. It prints the new ALB URL. If it times out, review
   `/var/log/driftgate-bootstrap.log` through SSM; stacks remain up for inspection.
   All remote READY commands are base64 encoded before submission to SSM.
2. Confirm the SNS subscription email. `make pipeline-status` prints the newest
   run's id, commit, stage statuses and timestamps, approval state, deployment id
   and S3 build log URI. Wait for any startup push run to finish before the first
   demo; approve it if needed. Each run's approval email is separate from the SNS
   subscription confirmation.
3. Run `make demo-push F=CLEAN`. Follow `make pipeline-status`, approve the run's
   SNS link when Build passes, and wait for a successful deployment. Verify
   `curl -f http://<ALB DNS>/health`. Save the printed commit id, run record and
   deployment id as the last good revision evidence.
4. Run `make demo-push F=F1`. Capture the D2 build failure and build log URI from
   `make pipeline-status`; confirm the run ends before Approval/Deploy and the
   ALB still serves the clean revision. Wait for the run to finish.
5. Run `make demo-push F=F9`. Approve if Build passes, then capture ValidateService,
   the gate verdict and the deployment outcome. Check whether the latency ratio
   exceeds the frozen 1.37 threshold. If it does not, record the null result as
   required by the session-2 deviation; do not change the threshold. If it fails,
   capture CodeDeploy's rollback deployment id and verify recovery at the ALB.
6. Alarm drill: start another `make demo-push F=CLEAN`, approve it, and wait until
   CodeDeploy is actively deploying. Force the configured alarm while that
   deployment is in progress:

   ```sh
   aws cloudwatch set-alarm-state --region us-east-1 \
     --alarm-name driftgate-serving-gate-score --state-value ALARM \
     --state-reason 'Session 3 rollback drill'
   ```

   Capture `DEPLOYMENT_STOP_ON_ALARM`, the automatic rollback deployment and the
   CloudWatch dashboard. Confirm the last good revision serves again. Let the
   metric evaluation restore OK, or clear the drill state with the same command
   using `--state-value OK --state-reason 'Session 3 drill complete'`, and verify
   the alarm is OK before another deployment.
7. Run `make demo-reset`. It pushes a commit whose tree equals `origin/main`
   exactly. Follow that run to completion (including approval if requested),
   then check the ALB. Collect the evidence before teardown.
8. Run `make session-down`, check `make lab-status`, then end the Lab session as
   described below. `session-up` never tears resources down.

Demo commands use Git plumbing with a temporary index. They fetch `origin/main`
for the base tree and use the current `codecommit/main` as the new commit's parent,
then push normally. CLEAN appends a UTC timestamped line to `docs/demo-log.md`;
fault pushes overlay only the injector's `serving/` files from that clean tree.
Faults supported by `python -m faults.inject` work here (F8 is study-runner-only).
Every command prints its new commit id and leaves the local branch, index and
working files intact. A concurrent CodeCommit push is rejected; rerun after
inspecting it. No command force-pushes. After demos, CodeCommit has its own
history, even after `demo-reset`; a later `session-up` seed push may be rejected
as non-fast-forward. Keep that history and use `demo-reset` to restore the
GitHub tree; do not force-push to seed it again.

## End the session

1. Finish the active pipeline run and collect the evidence you need. Pause further
   CodeCommit pushes while the session stacks are down.
2. Run `make session-down` while your credentials are still valid. It deletes the
   CodeDeploy stack and waits for it to finish before deleting the network stack,
   which owns exports the CodeDeploy stack imports.
3. Check `make lab-status` for remaining stacks and instances, then end the Lab
   session. The persistent storage and orchestrator stacks are retained for reuse.

The NAT gateway and ALB keep billing after the Lab stops instances. Delete the
session stacks before ending each Lab session to stop those charges.
