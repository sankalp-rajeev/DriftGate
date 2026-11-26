#!/usr/bin/env bash
# Lab only: start a session; leave all teardown to the owner.
set -euo pipefail
email="${APPROVAL_EMAIL:-}"
if [[ -z "${email//[[:space:]]/}" ]]; then
  echo "APPROVAL_EMAIL is required" >&2
  exit 2
fi
export AWS_REGION="${AWS_REGION:-us-east-1}"
export STACK_NAME="${STACK_NAME:-driftgate-net}"
export CD_STACK_NAME="${CD_STACK_NAME:-driftgate-cd}"

aws sts get-caller-identity --region "$AWS_REGION"
git fetch origin main
remote_main="$(git ls-remote codecommit refs/heads/main)"
synced=0
if [[ -z "$remote_main" ]]; then
  git push codecommit refs/remotes/origin/main:refs/heads/main
  synced=1
else
  git fetch codecommit main
  if [[ "$(git rev-parse 'origin/main^{tree}')" != "$(git rev-parse 'FETCH_HEAD^{tree}')" ]]; then
    bash "$(dirname "$0")/demo_push.sh" RESET
    synced=1
  else
    echo "CodeCommit main already matches origin/main's tree."
  fi
fi
if [[ "$synced" = 1 ]]; then
  echo "The pipeline run triggered by this sync is expected; its approval email can be rejected."
fi
"${MAKE:-make}" up
"${MAKE:-make}" cd-up
"${MAKE:-make}" orch-up

output() {
  aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text
}
host="$(output "$CD_STACK_NAME" BuildHostId)"
[[ -n "$host" && "$host" != None ]] || { echo "No build host in $CD_STACK_NAME" >&2; exit 1; }
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
deadline=$(($(date +%s) + 1800))
command_id=""
echo "Waiting up to 30 minutes for build host $host READY via SSM" >&2
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  if [[ -z "$command_id" ]]; then
    online="$(aws ssm describe-instance-information --region "$AWS_REGION" \
      --filters "Key=InstanceIds,Values=$host" \
      --query 'InstanceInformationList[0].PingStatus' --output text)"
    if [[ "$online" != Online ]]; then sleep 15; continue; fi
    encoded="$(base64 -w0 <<'COMMAND'
export HOME=/root PATH=/usr/local/bin:$PATH
set -eu
while ! test -f /opt/driftgate/.bootstrap-done; do sleep 5; done
echo 'DriftGate build host READY'
COMMAND
    )"
    remaining=$((deadline - $(date +%s)))
    [[ "$remaining" -gt 0 ]] || break
    printf '{"commands":["echo %s | base64 --decode | bash"],"executionTimeout":["%s"]}\n' \
      "$encoded" "$remaining" > "$work/parameters.json"
    command_id="$(aws ssm send-command --region "$AWS_REGION" --instance-ids "$host" \
      --document-name AWS-RunShellScript --parameters "file://$work/parameters.json" \
      --query Command.CommandId --output text)"
  fi
  status="$(aws ssm get-command-invocation --region "$AWS_REGION" \
    --command-id "$command_id" --instance-id "$host" --query Status --output text 2>"$work/error")" || {
    # Run Command may not have created the invocation immediately after send-command.
    if ! grep -q InvocationDoesNotExist "$work/error"; then cat "$work/error" >&2; exit 1; fi
    sleep 5
    continue
  }
  case "$status" in
    Success)
      aws ssm get-command-invocation --region "$AWS_REGION" --command-id "$command_id" \
        --instance-id "$host" --query StandardOutputContent --output text
      alb="$(output "$STACK_NAME" AlbDnsName)"
      [[ -n "$alb" && "$alb" != None ]] || { echo "No ALB DNS in $STACK_NAME" >&2; exit 1; }
      printf 'ALB URL: http://%s\nConfirm the SNS subscription email sent to %s before a demo push.\n' \
        "$alb" "$APPROVAL_EMAIL"
      exit 0
      ;;
    Pending|InProgress|Delayed) sleep 15 ;;
    *)
      echo "Build host READY check failed: $status; review /var/log/driftgate-bootstrap.log" >&2
      exit 1
      ;;
  esac
done
echo "Build host not READY within 30 minutes; review /var/log/driftgate-bootstrap.log" >&2
exit 1
