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
| Domain + cert + 443 listener | `rca.ingren.ai` (Route 53 zone in-account), ACM cert ISSUED, listener live, `/healthz` ok | 2026-07-23 |
| Slack app switch | Request URL verified, `app_mention` subscribed, `SLACK_APP_TOKEN` deleted, laptop daemon killed | 2026-07-23 |

## Pending

| Step | Lands with |
|---|---|
| SNS alarm subscription (email/phone confirm) | #13 |

## HTTPS for the ingress (#11)

Slack requires an HTTPS Request URL, so the ALB listener needs an ACM
certificate, and the certificate needs a domain. All manual, in order:

1. Pick a hostname you control, e.g. `rca.<your-domain>`.
2. Request the cert (must be in ap-south-1, same region as the ALB):

       aws acm request-certificate --profile ingren --region ap-south-1 \
         --domain-name rca.<your-domain> --validation-method DNS

3. Add the DNS validation CNAME that ACM prints to your DNS. Wait for
   `ISSUED` (`aws acm describe-certificate`).
4. CNAME `rca.<your-domain>` to the ALB DNS name (provision.sh prints it).
5. Put the cert ARN in `rca/.env` as `RCA_ALB_CERT_ARN`, re-run
   provision.sh. The 443 listener lands on that run.

## Slack app switch (#11)

After the listener answers:

1. In the Slack app config, copy the Signing Secret; add it to `rca/.env`
   as `SLACK_SIGNING_SECRET`; re-run provision.sh (lands in SSM).
2. Event Subscriptions -> Request URL:
   `https://rca.<your-domain>/slack/events`. Slack sends the
   `url_verification` challenge; ingress echoes it.
3. Subscribed bot events: `app_mention` only.
4. Delete the `SLACK_APP_TOKEN` (Socket Mode is gone). Closes #6's last
   box.
5. Stop the laptop daemon for good: `pkill -f daemon.py`.

## Image build and push (#9)

The image is ARM64: built on the laptop (Apple Silicon), run on Fargate
ARM. From the repo root:

    aws ecr get-login-password --profile ingren --region ap-south-1 \
      | docker login --username AWS --password-stdin 537124933640.dkr.ecr.ap-south-1.amazonaws.com
    docker buildx build --platform linux/arm64 \
      -t 537124933640.dkr.ecr.ap-south-1.amazonaws.com/rca:latest --push rca/

The task definition pins `:latest`; a push then a new `StartExecution`
picks it up. No redeploy step exists yet — one task per run.

## Poller deploy (#10)

The poller is an ECS Service pinned to one task. Order matters on first
provision: push an image that contains `poller/` first, then run
`provision.sh` — a Service started against an image without the code
crash-loops. After later code changes, push and then roll the Service:

    aws ecs update-service --profile ingren --region ap-south-1 \
      --cluster rca --service rca-poller --force-new-deployment

## Running the script

From the repo root:

    uv run --project rca --env-file rca/.env bash infra/provision.sh

uv loads the env file (bash `source` disagreed with its format). The
script pins profile `ingren` and region `ap-south-1`, refuses any other
account, and is safe to re-run. It prints one line per resource it
touched.
