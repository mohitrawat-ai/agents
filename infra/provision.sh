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

  # Slack signing secret lands with #11's app switch; push when present.
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

ACCOUNT_ID="537124933640"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${RCA_REGION}.amazonaws.com/rca"

ecr_and_logs() {
  "${AWS[@]}" ecr describe-repositories --repository-names rca >/dev/null 2>&1 \
    || "${AWS[@]}" ecr create-repository --repository-name rca >/dev/null
  log "ecr rca ($ECR_URI)"

  "${AWS[@]}" logs create-log-group --log-group-name /ecs/rca 2>/dev/null || true
  "${AWS[@]}" logs put-retention-policy --log-group-name /ecs/rca \
    --retention-in-days 30
  log "log group /ecs/rca (30d retention)"
}

# --- IAM: execution, task, and SFN roles (#9) -------------------------------
# Three roles, least privilege each (P9 §4 lives in the task definition, but
# the task ROLE is what the container can do as itself):
#   rca-task-execution  — ECS agent pulls the image, writes logs, reads /rca/*
#                         SSM params to inject secrets. The CONTAINER never
#                         holds these permissions.
#   rca-investigator    — what the running container may do: assume the
#                         partner readonly role, nothing else. aws_log's
#                         credential_source=EcsContainer resolves to this.
#   rca-sfn             — the state machine runs/stops/watches ECS tasks and
#                         passes the two roles above to them.

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
}

# --- Cluster + network (#9) -------------------------------------------------
# Default VPC, public subnets, egress-only security group, public IP on the
# task. Cheapest shape that reaches the internet (Anthropic, Neon, NR, AWS
# APIs): no NAT gateway, no ALB yet. Ingress lands with #11/#13.

CLUSTER=rca

network() {
  "${AWS[@]}" ecs describe-clusters --clusters "$CLUSTER" \
      --query 'clusters[0].status' --output text 2>/dev/null | grep -q ACTIVE \
    || "${AWS[@]}" ecs create-cluster --cluster-name "$CLUSTER" >/dev/null
  log "ecs cluster $CLUSTER"

  VPC_ID=$("${AWS[@]}" ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)
  SUBNET_IDS=$("${AWS[@]}" ec2 describe-subnets \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'Subnets[].SubnetId' --output text)

  SG_ID=$("${AWS[@]}" ec2 describe-security-groups \
    --filters Name=group-name,Values=rca-task "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text)
  if [[ "$SG_ID" == "None" ]]; then
    SG_ID=$("${AWS[@]}" ec2 create-security-group --group-name rca-task \
      --description "rca tasks: no ingress, default egress" \
      --vpc-id "$VPC_ID" --query GroupId --output text)
  fi
  log "network: vpc $VPC_ID, sg $SG_ID, subnets: $SUBNET_IDS"
}

# --- Task definition: investigator (#9) --------------------------------------
# Secret SCOPING is here (P9 §4): no SLACK_BOT_TOKEN in this task. The
# container gets exactly what run.py and the tools read, nothing more.
# INCIDENT_ID and ATTEMPT are NOT here — the state machine sets them per
# run via ContainerOverrides. ARM64: the image is built on the laptop
# (Apple Silicon) and Fargate ARM is the cheaper tier.

task_definition() {
  require NEW_RELIC_REGION
  local p="arn:aws:ssm:${RCA_REGION}:${ACCOUNT_ID}:parameter/rca"
  local langfuse_host_env=""
  if [[ -n "${LANGFUSE_HOST:-}" ]]; then
    langfuse_host_env=', {"name": "LANGFUSE_HOST", "value": "'"$LANGFUSE_HOST"'"}'
  fi

  "${AWS[@]}" ecs register-task-definition --cli-input-json '{
    "family": "rca-investigator",
    "requiresCompatibilities": ["FARGATE"],
    "networkMode": "awsvpc",
    "cpu": "1024",
    "memory": "2048",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "executionRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-task-execution",
    "taskRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-investigator",
    "containerDefinitions": [{
      "name": "investigator",
      "image": "'"$ECR_URI"':latest",
      "essential": true,
      "environment": [
        {"name": "NEW_RELIC_REGION", "value": "'"$NEW_RELIC_REGION"'"}'"$langfuse_host_env"'
      ],
      "secrets": [
        {"name": "RCA_DATABASE_URL",      "valueFrom": "'"$p"'/db/agent-url"},
        {"name": "NEW_RELIC_ACCOUNT_ID",  "valueFrom": "'"$p"'/newrelic/account-id"},
        {"name": "NEW_RELIC_API_KEY",     "valueFrom": "'"$p"'/newrelic/api-key"},
        {"name": "ANTHROPIC_API_KEY",     "valueFrom": "'"$p"'/anthropic/api-key"},
        {"name": "PARTNER_ROLE_ARN",      "valueFrom": "'"$p"'/partner-aws/role-arn"},
        {"name": "PARTNER_EXTERNAL_ID",   "valueFrom": "'"$p"'/partner-aws/external-id"},
        {"name": "LANGFUSE_PUBLIC_KEY",   "valueFrom": "'"$p"'/langfuse/public-key"},
        {"name": "LANGFUSE_SECRET_KEY",   "valueFrom": "'"$p"'/langfuse/secret-key"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/rca",
          "awslogs-region": "'"$RCA_REGION"'",
          "awslogs-stream-prefix": "investigator"
        }
      }
    }]
  }' >/dev/null
  log "task definition rca-investigator (arm64, 1 vCPU / 2 GB)"
}

# --- State machine (#9, §8e) --------------------------------------------------
# One state: RunTask.sync. Retry is BLIND on States.TaskFailed, capped at 1
# retry = 2 attempts (§8a-B); the task itself refuses re-runs of policy
# stops (§8e, run.py). ATTEMPT = $$.State.RetryCount + 1 — RetryCount is 0
# on the first attempt. Execution name = incident id and input carries only
# {incident_id}; both are the ROUTER's contract (#11), not the machine's.

state_machine() {
  local subnets_json sm_def sm_arn
  subnets_json=$(printf '"%s",' $SUBNET_IDS); subnets_json=${subnets_json%,}

  sm_def=$(cat <<EOF
{
  "Comment": "RCA investigation: one Fargate task per incident. Blind retry x1 (design.md 8e); the task guards its own re-run.",
  "StartAt": "Investigate",
  "TimeoutSeconds": 7200,
  "States": {
    "Investigate": {
      "Type": "Task",
      "Resource": "arn:aws:states:::ecs:runTask.sync",
      "Parameters": {
        "Cluster": "$CLUSTER",
        "TaskDefinition": "rca-investigator",
        "LaunchType": "FARGATE",
        "NetworkConfiguration": {
          "AwsvpcConfiguration": {
            "Subnets": [$subnets_json],
            "SecurityGroups": ["$SG_ID"],
            "AssignPublicIp": "ENABLED"
          }
        },
        "Overrides": {
          "ContainerOverrides": [{
            "Name": "investigator",
            "Environment": [
              {"Name": "INCIDENT_ID", "Value.\$": "\$.incident_id"},
              {"Name": "ATTEMPT", "Value.\$": "States.Format('{}', States.MathAdd(\$\$.State.RetryCount, 1))"}
            ]
          }]
        }
      },
      "Retry": [{
        "ErrorEquals": ["States.TaskFailed"],
        "IntervalSeconds": 10,
        "MaxAttempts": 1
      }],
      "End": true
    }
  }
}
EOF
)

  sm_arn="arn:aws:states:${RCA_REGION}:${ACCOUNT_ID}:stateMachine:rca-investigation"
  if "${AWS[@]}" stepfunctions describe-state-machine \
       --state-machine-arn "$sm_arn" >/dev/null 2>&1; then
    "${AWS[@]}" stepfunctions update-state-machine \
      --state-machine-arn "$sm_arn" --definition "$sm_def" \
      --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/rca-sfn" >/dev/null
  else
    "${AWS[@]}" stepfunctions create-state-machine \
      --name rca-investigation --type STANDARD --definition "$sm_def" \
      --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/rca-sfn" >/dev/null
  fi
  log "state machine rca-investigation"
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

# --- Poller: role, task definition, Service (#10) ---------------------------
# One always-on task: narrates events rows, posts the terminal message.
# Secret scoping (P9 §4): poller DSN + bot token, nothing else. No partner
# vars, so entrypoint.sh writes no hb-role profile. The task role may only
# ask Step Functions whether runs ended: DescribeExecution on this
# machine's executions, and nothing else.
#
# The Service pins desired-count 1 with maximumPercent 100. ECS may never
# run two pollers at once, even mid-deploy — two tasks would post every
# line twice (#10 rejects a lease for the same reason). A deploy is
# therefore stop-then-start; the cursor makes catch-up idempotent.
#
# Sequencing: the image must contain poller/ before this Service starts,
# or it crash-loops. RUNBOOK covers the push-then-provision order.

poller() {
  local ecs_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  "${AWS[@]}" iam get-role --role-name rca-poller >/dev/null 2>&1 \
    || "${AWS[@]}" iam create-role --role-name rca-poller \
         --assume-role-policy-document "$ecs_trust" >/dev/null
  "${AWS[@]}" iam put-role-policy --role-name rca-poller \
    --policy-name describe-investigation-executions --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": "states:DescribeExecution",
        "Resource": "arn:aws:states:'"$RCA_REGION"':'"$ACCOUNT_ID"':execution:rca-investigation:*"
      }]}'
  log "iam role rca-poller"

  local p="arn:aws:ssm:${RCA_REGION}:${ACCOUNT_ID}:parameter/rca"
  "${AWS[@]}" ecs register-task-definition --cli-input-json '{
    "family": "rca-poller",
    "requiresCompatibilities": ["FARGATE"],
    "networkMode": "awsvpc",
    "cpu": "256",
    "memory": "512",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "executionRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-task-execution",
    "taskRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-poller",
    "containerDefinitions": [{
      "name": "poller",
      "image": "'"$ECR_URI"':latest",
      "command": ["python", "poller/main.py"],
      "essential": true,
      "environment": [
        {"name": "RCA_STATE_MACHINE_ARN",
         "value": "arn:aws:states:'"$RCA_REGION"':'"$ACCOUNT_ID"':stateMachine:rca-investigation"}
      ],
      "secrets": [
        {"name": "RCA_DATABASE_URL", "valueFrom": "'"$p"'/db/poller-url"},
        {"name": "SLACK_BOT_TOKEN",  "valueFrom": "'"$p"'/slack/bot-token"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/rca",
          "awslogs-region": "'"$RCA_REGION"'",
          "awslogs-stream-prefix": "poller"
        }
      }
    }]
  }' >/dev/null
  log "task definition rca-poller (arm64, 0.25 vCPU / 512 MB)"

  local subnets_csv
  subnets_csv=$(printf '%s,' $SUBNET_IDS); subnets_csv=${subnets_csv%,}
  if "${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services rca-poller \
       --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
    "${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service rca-poller \
      --task-definition rca-poller >/dev/null
  else
    "${AWS[@]}" ecs create-service --cluster "$CLUSTER" --service-name rca-poller \
      --task-definition rca-poller --desired-count 1 --launch-type FARGATE \
      --deployment-configuration "maximumPercent=100,minimumHealthyPercent=0" \
      --network-configuration "awsvpcConfiguration={subnets=[$subnets_csv],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
      >/dev/null
  fi
  log "service rca-poller (desired 1)"
}

# --- Service: ingress + router, ALB (#11) ------------------------------------
# One task definition, two containers, both from the same image:
#   ingress — uvicorn serving service.ingress:asgi on 8000. Verifies the
#             Slack signature, enqueues, returns 200. ALB targets it.
#   router  — python -m service.router. Consumes rca-inbound, upserts,
#             StartExecution. No inbound traffic.
# Two tasks, always warm (design §3). Two routers racing is safe by
# §8a-A's table; two ingresses is the point of the ALB.
#
# Secret scoping per container (P9 §4): ingress gets the signing secret
# only; the router gets the service DSN and the bot token. Neither gets
# partner vars, so entrypoint.sh writes no hb-role profile.
#
# The router is essential:false (review 2026-07-23): a router startup
# failure must not kill the healthy ingress beside it — that would 503
# the ALB on both replicas and lose alerts, the exact invariant-5
# failure. A dead router only stops draining the queue; messages wait,
# then DLQ.
#
# HTTPS is Slack's requirement, so the listener needs an ACM cert. Cert
# issuance and DNS are manual (RUNBOOK); until RCA_ALB_CERT_ARN is set
# the stack lands listener-less and the script says so.

SERVICE_QUEUE_URL="https://sqs.${RCA_REGION}.amazonaws.com/${ACCOUNT_ID}/rca-inbound"

service_stack() {
  require RCA_CHANNEL_ALLOWLIST
  local ecs_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

  "${AWS[@]}" iam get-role --role-name rca-service >/dev/null 2>&1 \
    || "${AWS[@]}" iam create-role --role-name rca-service \
         --assume-role-policy-document "$ecs_trust" >/dev/null
  "${AWS[@]}" iam put-role-policy --role-name rca-service \
    --policy-name inbound-and-start --policy-document '{
      "Version": "2012-10-17",
      "Statement": [
        {"Effect": "Allow",
         "Action": ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage"],
         "Resource": "arn:aws:sqs:'"$RCA_REGION"':'"$ACCOUNT_ID"':rca-inbound"},
        {"Effect": "Allow",
         "Action": "states:StartExecution",
         "Resource": "arn:aws:states:'"$RCA_REGION"':'"$ACCOUNT_ID"':stateMachine:rca-investigation"}
      ]}'
  log "iam role rca-service"

  # ALB security group: 443 from anywhere (Slack publishes no stable IPs);
  # the task SG accepts 8000 from the ALB SG only. Duplicate-rule errors on
  # re-run are expected and tolerated.
  ALB_SG=$("${AWS[@]}" ec2 describe-security-groups \
    --filters Name=group-name,Values=rca-alb "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text)
  if [[ "$ALB_SG" == "None" ]]; then
    ALB_SG=$("${AWS[@]}" ec2 create-security-group --group-name rca-alb \
      --description "rca ALB: 443 from anywhere" \
      --vpc-id "$VPC_ID" --query GroupId --output text)
  fi
  "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$ALB_SG" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0 2>/dev/null || true
  "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 8000 --source-group "$ALB_SG" 2>/dev/null || true
  log "security groups: alb $ALB_SG, task ingress 8000 from it"

  # Existence guards (review 2026-07-23): elbv2 creates are idempotent
  # only while every parameter matches; a later parameter change would
  # make a bare create fail the whole run.
  local alb_arn alb_dns tg_arn
  if ! alb_arn=$("${AWS[@]}" elbv2 describe-load-balancers --names rca \
      --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null); then
    alb_arn=$("${AWS[@]}" elbv2 create-load-balancer --name rca \
      --type application --scheme internet-facing \
      --subnets $SUBNET_IDS --security-groups "$ALB_SG" \
      --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  fi
  alb_dns=$("${AWS[@]}" elbv2 describe-load-balancers \
    --load-balancer-arns "$alb_arn" \
    --query 'LoadBalancers[0].DNSName' --output text)
  if ! tg_arn=$("${AWS[@]}" elbv2 describe-target-groups --names rca-ingress \
      --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null); then
    tg_arn=$("${AWS[@]}" elbv2 create-target-group --name rca-ingress \
      --protocol HTTP --port 8000 --vpc-id "$VPC_ID" --target-type ip \
      --health-check-path /healthz --matcher HttpCode=200 \
      --query 'TargetGroups[0].TargetGroupArn' --output text)
  fi
  log "alb rca ($alb_dns), target group rca-ingress"

  # The Service below requires the target group to be associated with the
  # ALB, and only a listener creates that association (learned live,
  # 2026-07-23: CreateService fails with InvalidParameterException on an
  # unassociated TG). So no cert -> no listener -> the Service block is
  # skipped too, and lands on the re-run that brings the cert.
  if [[ -z "${RCA_ALB_CERT_ARN:-}" ]]; then
    log "NO 443 LISTENER and NO SERVICE yet: set RCA_ALB_CERT_ARN once the"
    log "cert exists (RUNBOOK) and re-run; both land on that run"
    return 0
  fi
  local have_listener
  have_listener=$("${AWS[@]}" elbv2 describe-listeners \
    --load-balancer-arn "$alb_arn" \
    --query 'Listeners[?Port==`443`] | length(@)' --output text)
  if [[ "$have_listener" == "0" ]]; then
    "${AWS[@]}" elbv2 create-listener --load-balancer-arn "$alb_arn" \
      --protocol HTTPS --port 443 --certificates "CertificateArn=$RCA_ALB_CERT_ARN" \
      --default-actions "Type=forward,TargetGroupArn=$tg_arn" >/dev/null
  fi
  log "listener 443 (cert set)"

  local p="arn:aws:ssm:${RCA_REGION}:${ACCOUNT_ID}:parameter/rca"
  "${AWS[@]}" ecs register-task-definition --cli-input-json '{
    "family": "rca-service",
    "requiresCompatibilities": ["FARGATE"],
    "networkMode": "awsvpc",
    "cpu": "512",
    "memory": "1024",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "executionRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-task-execution",
    "taskRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/rca-service",
    "containerDefinitions": [
      {
        "name": "ingress",
        "image": "'"$ECR_URI"':latest",
        "essential": true,
        "command": ["uvicorn", "--factory", "service.ingress:asgi",
                    "--host", "0.0.0.0", "--port", "8000"],
        "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
        "environment": [
          {"name": "RCA_INBOUND_QUEUE_URL", "value": "'"$SERVICE_QUEUE_URL"'"}
        ],
        "secrets": [
          {"name": "SLACK_SIGNING_SECRET", "valueFrom": "'"$p"'/slack/signing-secret"}
        ],
        "logConfiguration": {
          "logDriver": "awslogs",
          "options": {
            "awslogs-group": "/ecs/rca",
            "awslogs-region": "'"$RCA_REGION"'",
            "awslogs-stream-prefix": "service-ingress"
          }
        }
      },
      {
        "name": "router",
        "image": "'"$ECR_URI"':latest",
        "essential": false,
        "command": ["python", "-m", "service.router"],
        "environment": [
          {"name": "RCA_INBOUND_QUEUE_URL", "value": "'"$SERVICE_QUEUE_URL"'"},
          {"name": "RCA_STATE_MACHINE_ARN",
           "value": "arn:aws:states:'"$RCA_REGION"':'"$ACCOUNT_ID"':stateMachine:rca-investigation"},
          {"name": "RCA_CHANNEL_ALLOWLIST", "value": "'"$RCA_CHANNEL_ALLOWLIST"'"}
        ],
        "secrets": [
          {"name": "RCA_DATABASE_URL", "valueFrom": "'"$p"'/db/service-url"},
          {"name": "SLACK_BOT_TOKEN",  "valueFrom": "'"$p"'/slack/bot-token"}
        ],
        "logConfiguration": {
          "logDriver": "awslogs",
          "options": {
            "awslogs-group": "/ecs/rca",
            "awslogs-region": "'"$RCA_REGION"'",
            "awslogs-stream-prefix": "service-router"
          }
        }
      }
    ]
  }' >/dev/null
  log "task definition rca-service (2 containers)"

  local subnets_csv
  subnets_csv=$(printf '%s,' $SUBNET_IDS); subnets_csv=${subnets_csv%,}
  if "${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services rca-service \
       --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
    "${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service rca-service \
      --task-definition rca-service >/dev/null
  else
    "${AWS[@]}" ecs create-service --cluster "$CLUSTER" --service-name rca-service \
      --task-definition rca-service --desired-count 2 --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[$subnets_csv],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
      --load-balancers "targetGroupArn=$tg_arn,containerName=ingress,containerPort=8000" \
      --health-check-grace-period-seconds 60 \
      >/dev/null
  fi
  log "service rca-service (desired 2, behind alb)"
}

# Hard gate: wrong profile or region must stop the script, not scroll past.
account=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
[[ "$account" == "$ACCOUNT_ID" ]] || {
  echo "wrong account: $account (expected $ACCOUNT_ID)" >&2
  exit 1
}
log "account: $account, region: $RCA_REGION"
ssm_parameters
ecr_and_logs
iam_roles
network
task_definition
state_machine
poller
queues
service_stack
log "done"
