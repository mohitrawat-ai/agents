# Infra runbook — one-time manual steps (issue #15)

`provision.sh` creates every AWS resource. This file records what the
script cannot do: account-level and third-party steps, done by hand.
Acceptance rule: no resource exists that the script or these lines don't
record.

## Done

| Step | State | Date |
|---|---|---|
| AWS account | `537124933640`, IAM user `AdminIngren`, local profile `ingren` | pre-existing |
| Partner AWS access | cross-account role `ingren-rca-readonly` in `356367897942`, assumed with an ExternalId; local profile `hb-role` | pre-existing |
| Managed Postgres | Neon project, created by Mohit; role DSNs in `rca/.env` | 2026-07-21 |
| Region ruling | `ap-south-1` (Mumbai) | 2026-07-22 |

## Pending

| Step | Lands with |
|---|---|
| Slack app: delete `SLACK_APP_TOKEN`, add `SLACK_SIGNING_SECRET`, switch to HTTP Request URL | #11 |
| SNS alarm subscription (email/phone confirm) | #13 |

## Image build and push (#9)

The image is ARM64: built on the laptop (Apple Silicon), run on Fargate
ARM. From the repo root:

    aws ecr get-login-password --profile ingren --region ap-south-1 \
      | docker login --username AWS --password-stdin 537124933640.dkr.ecr.ap-south-1.amazonaws.com
    docker buildx build --platform linux/arm64 \
      -t 537124933640.dkr.ecr.ap-south-1.amazonaws.com/rca:latest --push rca/

The task definition pins `:latest`; a push then a new `StartExecution`
picks it up. No redeploy step exists yet — one task per run.

## Running the script

From the repo root:

    uv run --project rca --env-file rca/.env bash infra/provision.sh

uv loads the env file (bash `source` disagreed with its format). The
script pins profile `ingren` and region `ap-south-1`, refuses any other
account, and is safe to re-run. It prints one line per resource it
touched.
