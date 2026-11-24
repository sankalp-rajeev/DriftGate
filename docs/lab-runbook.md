# Learner Lab session runbook

Run these commands in the Learner Lab with current session credentials for `us-east-1`.
Use GitHub `main` after the Phase 0 PR is merged. The Lab repository defaults to
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
   review `/var/log/driftgate-bootstrap.log` if the marker is missing.
5. Run `make orch-up APPROVAL_EMAIL=you@example.org` and confirm the SNS subscription
   email when prompted. Use `make lab-status` to check all stack and instance states.

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
the build host's local reference directory before a build. The serving hooks fetch the
reference from S3.

The gate records verdicts in report mode until the t3.large calibration in Phase 2.
The laptop study's thresholds are archived separately.

The SSM agent writes Run Command output when `OutputS3BucketName` is set. Project
code cannot set the SSE header on those uploads. Verify live in Phase 2 or 3 that
SSM writes the command logs to the storage bucket under its encryption policy.

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
