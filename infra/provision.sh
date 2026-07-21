#!/usr/bin/env bash
# infra/provision.sh — checked-in AWS provisioning (issue #15, design.md §8c).
#
# One script of aws CLI commands creates every AWS resource the slices
# assume. It grows with the build: sections land as their issues need them.
# Re-running against an existing stack is safe — every command is either an
# overwrite or a create-if-absent. No Terraform; that changes only on a
# named failure (§8c).
#
# Mohit runs this, never Claude (standing rule: AWS resource creation is
# Mohit's to run). Invocation, from the repo root — uv loads .env with the
# parser already proven on this file (bash `source` disagreed with its
# format; 2026-07-22):
#
#     uv run --project rca --env-file rca/.env bash infra/provision.sh
#
# Overrides: RCA_PROFILE, RCA_REGION, RCA_PARTNER_PROFILE. The RCA_ prefix
# is deliberate — a bare REGION collided with a GCP var in the login shell.

set -euo pipefail

RCA_PROFILE="${RCA_PROFILE:-ingren}"        # our infra account, 537124933640
RCA_REGION="${RCA_REGION:-ap-south-1}"      # ruled 2026-07-22 (issue #15)
RCA_PARTNER_PROFILE="${RCA_PARTNER_PROFILE:-hb-role}"

AWS=(aws --profile "$RCA_PROFILE" --region "$RCA_REGION")

log() { printf '>> %s\n' "$*"; }

require() {
  local missing=0 v
  for v in "$@"; do
    [[ -n "${!v:-}" ]] || { echo "missing env: $v" >&2; missing=1; }
  done
  [[ $missing -eq 0 ]] || exit 1
}

put_param() { # put_param <ssm-name> <value>
  "${AWS[@]}" ssm put-parameter --name "$1" --type SecureString \
    --value "$2" --overwrite >/dev/null
  log "ssm $1"
}

# --- SSM parameters (this issue; consumed by task definitions in #9/#11) ----
# Per-task secret SCOPING is the task definition's job (P9 §4): parameters
# all exist here, but e.g. the bot token is never mounted into the
# investigation task. SLACK_SIGNING_SECRET lands with #11's app switch.

ssm_parameters() {
  require RCA_AGENT_DATABASE_URL RCA_SERVICE_DATABASE_URL \
    RCA_POLLER_DATABASE_URL NEW_RELIC_ACCOUNT_ID NEW_RELIC_API_KEY \
    ANTHROPIC_API_KEY SLACK_BOT_TOKEN

  put_param /rca/db/agent-url "$RCA_AGENT_DATABASE_URL"
  put_param /rca/db/service-url "$RCA_SERVICE_DATABASE_URL"
  put_param /rca/db/poller-url "$RCA_POLLER_DATABASE_URL"
  put_param /rca/newrelic/account-id "$NEW_RELIC_ACCOUNT_ID"
  put_param /rca/newrelic/api-key "$NEW_RELIC_API_KEY"
  put_param /rca/anthropic/api-key "$ANTHROPIC_API_KEY"
  put_param /rca/slack/bot-token "$SLACK_BOT_TOKEN"

  # Partner access is a cross-account role, not keys: we assume
  # ingren-rca-readonly in their account, gated by an ExternalId. No
  # long-lived partner credential exists anywhere. These two values are
  # config for that assume — #9 grants the task role sts:AssumeRole on
  # the ARN and ships an ~/.aws/config profile named hb-role with
  # credential_source = EcsContainer, so aws_log's --profile default
  # works unchanged. Read from the local profile so they are never typed.
  local arn xid
  arn=$(aws configure get role_arn --profile "$RCA_PARTNER_PROFILE")
  xid=$(aws configure get external_id --profile "$RCA_PARTNER_PROFILE")
  put_param /rca/partner-aws/role-arn "$arn"
  put_param /rca/partner-aws/external-id "$xid"

  # Langfuse mirror is optional (§8c): push only what exists locally.
  if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" ]]; then
    put_param /rca/langfuse/public-key "$LANGFUSE_PUBLIC_KEY"
  fi
  if [[ -n "${LANGFUSE_SECRET_KEY:-}" ]]; then
    put_param /rca/langfuse/secret-key "$LANGFUSE_SECRET_KEY"
  fi
  if [[ -n "${LANGFUSE_HOST:-}" ]]; then
    put_param /rca/langfuse/host "$LANGFUSE_HOST"
  fi
}

# Hard gate: wrong profile or region must stop the script, not scroll past.
account=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
[[ "$account" == "537124933640" ]] || {
  echo "wrong account: $account (expected 537124933640)" >&2
  exit 1
}
log "account: $account, region: $RCA_REGION"
ssm_parameters
log "done"
