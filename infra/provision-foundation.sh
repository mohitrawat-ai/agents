#!/usr/bin/env bash
# infra/provision-foundation.sh — the free, static base. Split out of
# provision.sh on 2026-07-22 (issue #15); every function is moved
# unchanged, minus the pieces lib.sh now owns.
#
# Sections: SSM parameters, ECR + logs, IAM roles (execution, task, SFN,
# poller, service), cluster + network, queues. Everything here idles at
# ~zero cost and changes rarely. Run once, re-run only when a secret or a
# policy changes. Safe to re-run: every command is an overwrite or a
# create-if-absent.
#
# Invocation, from the repo root (uv loads .env; bash `source` disagreed
# with its format, 2026-07-22):
#
#     uv run --project rca --env-file rca/.env bash infra/provision-foundation.sh

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# --- SSM parameters (consumed by task definitions) --------------------------
# Per-task secret SCOPING is the task definition's job (P9 §4): parameters
# all exist here, but e.g. the bot token is never mounted into the
# investigation task.

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

  if [[ -n "${SLACK_SIGNING_SECRET:-}" ]]; then
    put_param /rca/slack/signing-secret "$SLACK_SIGNING_SECRET"
  fi

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

# --- ECR + logs (#9) --------------------------------------------------------

ecr_and_logs() {
  "${AWS[@]}" ecr describe-repositories --repository-names rca >/dev/null 2>&1 \
    || "${AWS[@]}" ecr create-repository --repository-name rca >/dev/null
  log "ecr rca ($ECR_URI)"

  "${AWS[@]}" logs create-log-group --log-group-name /ecs/rca 2>/dev/null || true
  "${AWS[@]}" logs put-retention-policy --log-group-name /ecs/rca \
    --retention-in-days 30
  log "log group /ecs/rca (30d retention)"
}

# --- IAM: every role (#9, #10, #11) -----------------------------------------
# Least privilege each (P9 §4 lives in the task definition, but the task
# ROLE is what the container can do as itself):
#   rca-task-execution  — ECS agent pulls the image, writes logs, reads /rca/*
#                         SSM params to inject secrets. The CONTAINER never
#                         holds these permissions.
#   rca-investigator    — what the running container may do: assume the
#                         partner readonly role, nothing else.
#   rca-sfn             — the state machine runs/stops/watches ECS tasks and
#                         passes the two roles above to them.
#   rca-poller          — DescribeExecution on this machine's executions only.
#   rca-service         — inbound queue send/receive/delete + StartExecution.

iam_roles() {
  local ecs_trust sfn_trust
  ecs_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  sfn_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"states.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

  ensure_role() { # ensure_role <name> <trust-json>
    "${AWS[@]}" iam get-role --role-name "$1" >/dev/null 2>&1 \
      || "${AWS[@]}" iam create-role --role-name "$1" \
           --assume-role-policy-document "$2" >/dev/null
    log "iam role $1"
  }

  ensure_role rca-task-execution "$ecs_trust"
  "${AWS[@]}" iam attach-role-policy --role-name rca-task-execution \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  "${AWS[@]}" iam put-role-policy --role-name rca-task-execution \
    --policy-name read-rca-ssm --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": "ssm:GetParameters",
        "Resource": "arn:aws:ssm:'"$RCA_REGION"':'"$ACCOUNT_ID"':parameter/rca/*"
      }]}'

  local partner_arn
  partner_arn=$(aws configure get role_arn --profile "$RCA_PARTNER_PROFILE")
  ensure_role rca-investigator "$ecs_trust"
  "${AWS[@]}" iam put-role-policy --role-name rca-investigator \
    --policy-name assume-partner-readonly --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Resource": "'"$partner_arn"'"
      }]}'

  ensure_role rca-sfn "$sfn_trust"
  "${AWS[@]}" iam put-role-policy --role-name rca-sfn \
    --policy-name run-investigation-task --policy-document '{
      "Version": "2012-10-17",
      "Statement": [
        {"Effect": "Allow",
         "Action": ["ecs:RunTask", "ecs:StopTask", "ecs:DescribeTasks"],
         "Resource": "*"},
        {"Effect": "Allow",
         "Action": ["events:PutTargets", "events:PutRule", "events:DescribeRule"],
         "Resource": "arn:aws:events:'"$RCA_REGION"':'"$ACCOUNT_ID"':rule/StepFunctionsGetEventsForECSTaskRule"},
        {"Effect": "Allow",
         "Action": "iam:PassRole",
         "Resource": [
           "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-task-execution",
           "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-investigator"
         ]}
      ]}'

  ensure_role rca-poller "$ecs_trust"
  "${AWS[@]}" iam put-role-policy --role-name rca-poller \
    --policy-name describe-investigation-executions --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": "states:DescribeExecution",
        "Resource": "arn:aws:states:'"$RCA_REGION"':'"$ACCOUNT_ID"':execution:rca-investigation:*"
      }]}'

  ensure_role rca-service "$ecs_trust"
  "${AWS[@]}" iam put-role-policy --role-name rca-service \
    --policy-name inbound-and-start --policy-document '{
      "Version": "2012-10-17",
      "Statement": [
        {"Effect": "Allow",
         "Action": ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage"],
         "Resource": "arn:aws:sqs:'"$RCA_REGION"':'"$ACCOUNT_ID"':rca-inbound"},
        {"Effect": "Allow",
         "Action": "states:StartExecution",
         "Resource": "'"$SM_ARN"'"}
      ]}'
}

# --- Cluster + network (#9) -------------------------------------------------
# Default VPC, public subnets, egress-only security group, public IP on the
# task. Cheapest shape that reaches the internet (Anthropic, Neon, NR, AWS
# APIs): no NAT gateway. The ALB and its SG live in services.sh — they are
# the cost axis, not the base.

network() {
  "${AWS[@]}" ecs describe-clusters --clusters "$CLUSTER" \
      --query 'clusters[0].status' --output text 2>/dev/null | grep -q ACTIVE \
    || "${AWS[@]}" ecs create-cluster --cluster-name "$CLUSTER" >/dev/null
  log "ecs cluster $CLUSTER"

  VPC_ID=$("${AWS[@]}" ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)

  SG_ID=$("${AWS[@]}" ec2 describe-security-groups \
    --filters Name=group-name,Values=rca-task "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text)
  if [[ "$SG_ID" == "None" ]]; then
    SG_ID=$("${AWS[@]}" ec2 create-security-group --group-name rca-task \
      --description "rca tasks: no ingress, default egress" \
      --vpc-id "$VPC_ID" --query GroupId --output text)
  fi
  log "network: vpc $VPC_ID, sg $SG_ID"
}

# --- Inbound queue + DLQ (#11, §8a-A) ----------------------------------------
# ONE queue. Raw Slack envelopes in; the router consumes and calls
# StartExecution directly — §8a-A dropped the second queue. Standard, not
# FIFO: ordering is not needed (the event_id guard and execution-name
# idempotency absorb duplicates), and FIFO throughput limits buy nothing.
#
# Redelivery math is load-bearing for the poller's never-started grace
# (poller/main.py, 1800s): visibility 60s x maxReceiveCount 5 means a
# persistently failing StartExecution exhausts to the DLQ in ~5 minutes,
# comfortably inside the grace window. Change either number and re-check
# that inequality.
#
# Long polling (ReceiveMessageWaitTimeSeconds 20) so the router's receive
# loop is cheap. Retention 4 days on both: enough to notice and redrive a
# DLQ'd alert after a weekend.

queues() {
  local dlq_url dlq_arn q_url
  "${AWS[@]}" sqs create-queue --queue-name rca-inbound-dlq \
    --attributes '{"MessageRetentionPeriod": "345600"}' >/dev/null
  dlq_url=$("${AWS[@]}" sqs get-queue-url --queue-name rca-inbound-dlq \
    --query QueueUrl --output text)
  dlq_arn=$("${AWS[@]}" sqs get-queue-attributes --queue-url "$dlq_url" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)
  log "queue rca-inbound-dlq"

  "${AWS[@]}" sqs create-queue --queue-name rca-inbound --attributes '{
      "MessageRetentionPeriod": "345600",
      "VisibilityTimeout": "60",
      "ReceiveMessageWaitTimeSeconds": "20",
      "RedrivePolicy": "{\"deadLetterTargetArn\":\"'"$dlq_arn"'\",\"maxReceiveCount\":\"5\"}"
    }' >/dev/null
  q_url=$("${AWS[@]}" sqs get-queue-url --queue-name rca-inbound \
    --query QueueUrl --output text)
  log "queue rca-inbound ($q_url)"
}

ssm_parameters
ecr_and_logs
iam_roles
network
queues
log "foundation done"
