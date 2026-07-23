#!/usr/bin/env bash
# infra/provision-definitions.sh — the free, hot layer. Split out of
# provision.sh on 2026-07-22 (issue #15): three task definitions and the
# state machine. These change with nearly every issue and cost nothing,
# so they get their own script — re-run this after editing, without
# touching the base or the running services.
#
# A Service picks up a new task-def revision only on its next deployment
# (services.sh up, or update-service). A new investigator revision is
# picked up by the next StartExecution — the machine names the family,
# ECS resolves latest ACTIVE revision at RunTask.
#
# Note: re-running registers a new revision even when nothing changed.
# Harmless noise, accepted.
#
# Invocation, from the repo root:
#
#     uv run --project rca --env-file rca/.env bash infra/provision-definitions.sh

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
network_lookups

# --- Task definition: investigator (#9) --------------------------------------
# Secret SCOPING is here (P9 §4): no SLACK_BOT_TOKEN in this task. The
# container gets exactly what run.py and the tools read, nothing more.
# INCIDENT_ID and ATTEMPT are NOT here — the state machine sets them per
# run via ContainerOverrides. ARM64: the image is built on the laptop
# (Apple Silicon) and Fargate ARM is the cheaper tier.

investigator_definition() {
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
        {"name": "NEW_RELIC_REGION", "value": "'"$NEW_RELIC_REGION"'"},
        {"name": "RCA_SESSION_BUCKET", "value": "rca-sessions-'"$ACCOUNT_ID"'"}'"$langfuse_host_env"'
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

# --- Task definition: poller (#10) -------------------------------------------
# Secret scoping (P9 §4): poller DSN + bot token, nothing else. No partner
# vars, so entrypoint.sh writes no hb-role profile. The task role may only
# ask Step Functions whether runs ended.

poller_definition() {
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
        {"name": "RCA_STATE_MACHINE_ARN", "value": "'"$SM_ARN"'"}
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
}

# --- Task definition: ingress + router (#11) ---------------------------------
# One task definition, two containers, both from the same image:
#   ingress — uvicorn serving service.ingress:asgi on 8000. Verifies the
#             Slack signature, enqueues, returns 200. ALB targets it.
#   router  — python -m service.router. Consumes rca-inbound, upserts,
#             StartExecution. No inbound traffic.
#
# Secret scoping per container (P9 §4): ingress gets the signing secret
# only; the router gets the service DSN and the bot token. Neither gets
# partner vars, so entrypoint.sh writes no hb-role profile.
#
# The router is essential:false (review 2026-07-22): a router startup
# failure must not kill the healthy ingress beside it — that would 503
# the ALB on both replicas and lose alerts, the exact invariant-5
# failure. A dead router only stops draining the queue; messages wait,
# then DLQ.

service_definition() {
  require RCA_CHANNEL_ALLOWLIST
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
          {"name": "RCA_QA_QUEUE_URL",      "value": "'"$QA_QUEUE_URL"'"},
          {"name": "RCA_STATE_MACHINE_ARN", "value": "'"$SM_ARN"'"},
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
      },
      {
        "name": "qa-worker",
        "image": "'"$ECR_URI"':latest",
        "essential": false,
        "command": ["python", "-m", "qa.worker"],
        "environment": [
          {"name": "RCA_QA_QUEUE_URL", "value": "'"$QA_QUEUE_URL"'"}
        ],
        "secrets": [
          {"name": "RCA_DATABASE_URL",  "valueFrom": "'"$p"'/db/service-url"},
          {"name": "SLACK_BOT_TOKEN",   "valueFrom": "'"$p"'/slack/bot-token"},
          {"name": "ANTHROPIC_API_KEY", "valueFrom": "'"$p"'/anthropic/api-key"}
        ],
        "logConfiguration": {
          "logDriver": "awslogs",
          "options": {
            "awslogs-group": "/ecs/rca",
            "awslogs-region": "'"$RCA_REGION"'",
            "awslogs-stream-prefix": "service-qa-worker"
          }
        }
      }
    ]
  }' >/dev/null
  log "task definition rca-service (3 containers)"
}

# --- State machine (#9, §8e) --------------------------------------------------
# One state: RunTask.sync. Retry is BLIND on States.TaskFailed, capped at 1
# retry = 2 attempts (§8a-B); the task itself refuses re-runs of policy
# stops (§8e, run.py). ATTEMPT = $$.State.RetryCount + 1 — RetryCount is 0
# on the first attempt. Execution name = incident id and input carries only
# {incident_id}; both are the ROUTER's contract (#11), not the machine's.

state_machine() {
  local subnets_json sm_def
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

  if "${AWS[@]}" stepfunctions describe-state-machine \
       --state-machine-arn "$SM_ARN" >/dev/null 2>&1; then
    "${AWS[@]}" stepfunctions update-state-machine \
      --state-machine-arn "$SM_ARN" --definition "$sm_def" \
      --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/rca-sfn" >/dev/null
  else
    "${AWS[@]}" stepfunctions create-state-machine \
      --name rca-investigation --type STANDARD --definition "$sm_def" \
      --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/rca-sfn" >/dev/null
  fi
  log "state machine rca-investigation"
}

investigator_definition
poller_definition
service_definition
state_machine
log "definitions done"
