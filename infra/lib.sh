#!/usr/bin/env bash
# infra/lib.sh — shared prelude for the provision scripts. Split out of
# provision.sh on 2026-07-22 (issue #15): three scripts along two axes,
# idle cost and change frequency. Sourced, never executed.
#
# Provides: the AWS wrapper, the account hard-gate, log/require/put_param,
# shared names (cluster, ECR, queue URL), and network_lookups(), which
# re-derives VPC_ID / SUBNET_IDS / SG_ID for scripts that run after
# foundation created them. Lookups are read-only; creation stays in
# provision-foundation.sh.

set -euo pipefail

RCA_PROFILE="${RCA_PROFILE:-ingren}"        # our infra account, 537124933640
RCA_REGION="${RCA_REGION:-ap-south-1}"      # ruled 2026-07-22 (issue #15)
RCA_PARTNER_PROFILE="${RCA_PARTNER_PROFILE:-hb-role}"

AWS=(aws --profile "$RCA_PROFILE" --region "$RCA_REGION")

ACCOUNT_ID="537124933640"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${RCA_REGION}.amazonaws.com/rca"
CLUSTER=rca
SERVICE_QUEUE_URL="https://sqs.${RCA_REGION}.amazonaws.com/${ACCOUNT_ID}/rca-inbound"
QA_QUEUE_URL="https://sqs.${RCA_REGION}.amazonaws.com/${ACCOUNT_ID}/rca-qa.fifo"
SM_ARN="arn:aws:states:${RCA_REGION}:${ACCOUNT_ID}:stateMachine:rca-investigation"

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

# Read-only re-derivation of what foundation created. Dies if the SG is
# missing so a definitions/services run against a bare account fails with
# a named cause instead of a null in a task definition.
network_lookups() {
  VPC_ID=$("${AWS[@]}" ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)
  SUBNET_IDS=$("${AWS[@]}" ec2 describe-subnets \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'Subnets[].SubnetId' --output text)
  SG_ID=$("${AWS[@]}" ec2 describe-security-groups \
    --filters Name=group-name,Values=rca-task "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text)
  [[ "$SG_ID" != "None" ]] || {
    echo "security group rca-task not found: run provision-foundation.sh first" >&2
    exit 1
  }
}

# Hard gate: wrong profile or region must stop the script, not scroll past.
account=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
[[ "$account" == "$ACCOUNT_ID" ]] || {
  echo "wrong account: $account (expected $ACCOUNT_ID)" >&2
  exit 1
}
log "account: $account, region: $RCA_REGION"
