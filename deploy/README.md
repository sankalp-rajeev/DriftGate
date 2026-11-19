Build stage, CodeDeploy appspec and hooks, CloudFormation (FR-16 to FR-19, FR-21).

## Build host checkout (T-28, human setup)

The orchestrator (`orchestrator/config.json`, PRD 6.8) runs this on the build host through SSM
Run Command (as root):

    cd /opt/driftgate && git fetch && git checkout <commit> && deploy/build.sh --commit <commit> --run-id <id>

Git reaches CodeCommit through the CodeCommit credential helper and the instance profile
(LabInstanceProfile); no keys are stored. One-time setup on the host, as root:

    git config --system credential.helper '!aws codecommit credential-helper $@'
    git config --system credential.UseHttpPath true
    git clone https://git-codecommit.us-east-1.amazonaws.com/v1/repos/driftgate /opt/driftgate
