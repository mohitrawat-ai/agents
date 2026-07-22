# Infra runbook — one-time manual steps (issue #15)

The provision scripts create every AWS resource. This file records what
they cannot do: account-level and third-party steps, done by hand.
Acceptance rule: no resource exists that the scripts or these lines don't
record.

## The scripts

One script became three on 2026-07-22 (design.md §8c amendment), split on
idle cost and change frequency. All source `lib.sh` (account gate, shared
names, network lookups). All run the same way, from the repo root:

    uv run --project rca --env-file rca/.env bash infra/<script>

| Script | Holds | Idle cost | Run when |
|---|---|---|---|
| `provision-foundation.sh` | SSM params, ECR, logs, all IAM roles, cluster + task SG, queues | ~zero | Once; then on secret rotation or policy change |
| `provision-definitions.sh` | 3 task definitions, state machine | zero | After editing any of them |
| `services.sh up\|down\|status` | poller Service, ALB + listener + CNAME, rca-service | ~$40/mo when up | `up` to test, `down` between sessions |

Order on a bare account: foundation, definitions, image push, `services.sh up`.

uv loads the env file (bash `source` disagreed with its format). Every
script pins profile `ingren` and region `ap-south-1`, refuses any other
account, and is safe to re-run.

## services.sh up/down — what to know

- **down means off.** Alerts during downtime are not investigated. Only
  Mohit tags the bot for now, so this is a known, accepted state.
- **Slack may disable the event subscription during a long down.** The
  Request URL answers nothing; Slack retries, warns, and can switch the
  subscription off. After `up`, check the Slack app config → Event
  Subscriptions; re-verify the URL if it shows disabled.
- **The ALB DNS name rotates on recreate.** `up` re-UPSERTs the
  `rca.ingren.ai` CNAME itself (zone is in-account). The cert and the
  Slack Request URL point at the hostname and survive. Allow ~60s TTL
  plus ALB provisioning before the first event flows.
- **`up` is also the deploy step**: it rolls both Services to the latest
  task-def revision. After a definitions change while already up, either
  re-run `up` or roll by hand (see poller deploy below).
- **desired counts**: poller 1 (pinned, never two), rca-service 1
  (testing; design §3 says 2 — restore at go-live by editing `up`).

## Done

| Step | State | Date |
|---|---|---|
| AWS account | `537124933640`, IAM user `AdminIngren`, local profile `ingren` | pre-existing |
| Partner AWS access | cross-account role `ingren-rca-readonly` in `356367897942`, assumed with an ExternalId; local profile `hb-role` | pre-existing |
| Managed Postgres | Neon project, created by Mohit; role DSNs in `rca/.env` | 2026-07-21 |
| Region ruling | `ap-south-1` (Mumbai) | 2026-07-22 |
| Domain + cert + 443 listener | `rca.ingren.ai` (Route 53 zone in-account), ACM cert ISSUED, listener live, `/healthz` ok | 2026-07-22 |
| Slack app switch | Request URL verified, `app_mention` subscribed, `SLACK_APP_TOKEN` deleted, laptop daemon killed | 2026-07-22 |
| Script split | one provision.sh → foundation / definitions / services + lib | 2026-07-22 |

## Pending

| Step | Lands with |
|---|---|
| SNS alarm subscription (email/phone confirm) | #13 |

## HTTPS for the ingress (#11)

Slack requires an HTTPS Request URL, so the ALB listener needs an ACM
certificate, and the certificate needs a domain. All manual, in order
(done 2026-07-22; kept for a rebuild):

1. Pick a hostname you control, e.g. `rca.<your-domain>`.
2. Request the cert (must be in ap-south-1, same region as the ALB):

       aws acm request-certificate --profile ingren --region ap-south-1 \
         --domain-name rca.<your-domain> --validation-method DNS

3. Add the DNS validation CNAME that ACM prints to your DNS. Wait for
   `ISSUED` (`aws acm describe-certificate`).
4. Put the cert ARN in `rca/.env` as `RCA_ALB_CERT_ARN` — `services.sh up`
   requires it, creates the listener, and writes the CNAME itself.

## Slack app switch (#11)

**Learned live 2026-07-22:** the Socket Mode TOGGLE (Settings → Socket
Mode) must be switched OFF. Deleting the app-level token alone leaves
events routed to the dead socket, and the Request URL silently receives
nothing.

After the listener answers:

1. In the Slack app config, copy the Signing Secret; add it to `rca/.env`
   as `SLACK_SIGNING_SECRET`; re-run provision-foundation.sh (lands in
   SSM).
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

The task definitions pin `:latest`; a push then a new `StartExecution`
picks it up for the investigator. Running Services need a roll (below).

## Poller deploy (#10)

The poller is an ECS Service pinned to one task. Order matters on first
provision: push an image that contains `poller/` first, then `services.sh
up` — a Service started against an image without the code crash-loops.
After later code changes, push and then roll the Service:

    aws ecs update-service --profile ingren --region ap-south-1 \
      --cluster rca --service rca-poller --force-new-deployment
