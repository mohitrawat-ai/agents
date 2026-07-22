#!/usr/bin/env bash
# infra/services.sh up|down|status — the cost axis. Split out of
# provision.sh on 2026-07-22 (issue #15). Everything with an idle price
# lives here: the poller Service (~$7/mo), the ALB (~$18/mo), and
# rca-service (~$15/mo per task). All free resources stay up forever in
# the other two scripts; this one exists so the paid ones can be turned
# off between testing sessions.
#
#   up     — ALB + 443 listener + Route 53 CNAME, then both Services at
#            desired 1, on the latest task-def revisions.
#   down   — Services to desired 0, delete the ALB. Keeps the target
#            group, security groups, cert, and DNS record: all free, and
#            keeping the TG preserves rca-service's load-balancer config.
#   status — what exists and what it is costing.
#
# down means OFF: alerts during downtime are not investigated, and the
# Slack Request URL answers nothing. Slack retries, then may disable the
# event subscription — after a long down, re-verify it (RUNBOOK).
#
# The ALB's DNS name rotates on every recreate, so up re-UPSERTs the
# rca.ingren.ai CNAME. The cert and the Slack Request URL point at the
# hostname and survive.
#
# desired-count 1 for rca-service (ruled 2026-07-22): design §3 says two
# always-warm tasks, but one tester needs one. Restore 2 at go-live.
#
# Invocation, from the repo root:
#
#     uv run --project rca --env-file rca/.env bash infra/services.sh up

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

RCA_HOSTNAME="${RCA_HOSTNAME:-rca.ingren.ai}"
RCA_ZONE="${RCA_ZONE:-ingren.ai}"

# --- shared helpers ----------------------------------------------------------

set_desired() { # set_desired <service> <count> — no-op if service absent
  if "${AWS[@]}" ecs describe-services --cluster "$CLUSTER" --services "$1" \
       --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
    "${AWS[@]}" ecs update-service --cluster "$CLUSTER" --service "$1" \
      --task-definition "$1" --desired-count "$2" >/dev/null
    log "service $1 -> desired $2 (latest task def)"
  else
    log "service $1 absent"
    return 1
  fi
}

# --- up ----------------------------------------------------------------------

up() {
  require RCA_ALB_CERT_ARN
  network_lookups

  # ALB security group: 443 from anywhere (Slack publishes no stable IPs);
  # the task SG accepts 8000 from the ALB SG only. Duplicate-rule errors on
  # re-run are expected and tolerated.
  local alb_sg
  alb_sg=$("${AWS[@]}" ec2 describe-security-groups \
    --filters Name=group-name,Values=rca-alb "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text)
  if [[ "$alb_sg" == "None" ]]; then
    alb_sg=$("${AWS[@]}" ec2 create-security-group --group-name rca-alb \
      --description "rca ALB: 443 from anywhere" \
      --vpc-id "$VPC_ID" --query GroupId --output text)
  fi
  "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$alb_sg" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0 2>/dev/null || true
  "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 8000 --source-group "$alb_sg" 2>/dev/null || true
  log "security groups: alb $alb_sg, task ingress 8000 from it"

  local alb_arn alb_dns tg_arn
  if ! alb_arn=$("${AWS[@]}" elbv2 describe-load-balancers --names rca \
      --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null); then
    alb_arn=$("${AWS[@]}" elbv2 create-load-balancer --name rca \
      --type application --scheme internet-facing \
      --subnets $SUBNET_IDS --security-groups "$alb_sg" \
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

  # The Service create below requires the TG to be associated with the
  # ALB, and only a listener creates that association (learned live,
  # 2026-07-22). Listener before Services, always.
  local have_listener
  have_listener=$("${AWS[@]}" elbv2 describe-listeners \
    --load-balancer-arn "$alb_arn" \
    --query 'Listeners[?Port==`443`] | length(@)' --output text)
  if [[ "$have_listener" == "0" ]]; then
    "${AWS[@]}" elbv2 create-listener --load-balancer-arn "$alb_arn" \
      --protocol HTTPS --port 443 --certificates "CertificateArn=$RCA_ALB_CERT_ARN" \
      --default-actions "Type=forward,TargetGroupArn=$tg_arn" >/dev/null
  fi
  log "listener 443"

  # A fresh ALB has a fresh DNS name; repoint the hostname at it. The
  # zone is in-account (RUNBOOK), so this is scriptable. UPSERT is
  # idempotent when nothing changed.
  local zone_id
  zone_id=$("${AWS[@]}" route53 list-hosted-zones-by-name \
    --dns-name "$RCA_ZONE" --query 'HostedZones[0].Id' --output text)
  "${AWS[@]}" route53 change-resource-record-sets --hosted-zone-id "$zone_id" \
    --change-batch '{
      "Changes": [{
        "Action": "UPSERT",
        "ResourceRecordSet": {
          "Name": "'"$RCA_HOSTNAME"'.",
          "Type": "CNAME",
          "TTL": 60,
          "ResourceRecords": [{"Value": "'"$alb_dns"'"}]
        }
      }]}' >/dev/null
  log "route53: $RCA_HOSTNAME -> $alb_dns"

  # Poller: one always-on task, never two (deploy is stop-then-start,
  # maximumPercent 100 — two pollers would post every line twice, #10).
  local subnets_csv
  subnets_csv=$(printf '%s,' $SUBNET_IDS); subnets_csv=${subnets_csv%,}
  set_desired rca-poller 1 \
    || "${AWS[@]}" ecs create-service --cluster "$CLUSTER" --service-name rca-poller \
         --task-definition rca-poller --desired-count 1 --launch-type FARGATE \
         --deployment-configuration "maximumPercent=100,minimumHealthyPercent=0" \
         --network-configuration "awsvpcConfiguration={subnets=[$subnets_csv],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
         >/dev/null
  set_desired rca-service 1 \
    || "${AWS[@]}" ecs create-service --cluster "$CLUSTER" --service-name rca-service \
         --task-definition rca-service --desired-count 1 --launch-type FARGATE \
         --network-configuration "awsvpcConfiguration={subnets=[$subnets_csv],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
         --load-balancers "targetGroupArn=$tg_arn,containerName=ingress,containerPort=8000" \
         --health-check-grace-period-seconds 60 \
         >/dev/null
  log "up: poller 1, service 1, alb live. Slack event subscription may need"
  log "re-verify if this followed a long down (RUNBOOK)."
}

# --- down --------------------------------------------------------------------

down() {
  set_desired rca-poller 0 || true
  set_desired rca-service 0 || true

  local alb_arn
  if alb_arn=$("${AWS[@]}" elbv2 describe-load-balancers --names rca \
      --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null); then
    "${AWS[@]}" elbv2 delete-load-balancer --load-balancer-arn "$alb_arn"
    log "alb deleted (listeners go with it)"
  else
    log "alb already absent"
  fi
  log "down: idle cost is now ~zero. Kept: target group, SGs, cert, DNS"
  log "record, task defs, everything free. Alerts will NOT be investigated."
}

# --- status ------------------------------------------------------------------

status() {
  local alb_dns
  if alb_dns=$("${AWS[@]}" elbv2 describe-load-balancers --names rca \
      --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null); then
    log "alb: up ($alb_dns) ~\$18/mo"
  else
    log "alb: absent"
  fi
  "${AWS[@]}" ecs describe-services --cluster "$CLUSTER" \
    --services rca-poller rca-service \
    --query 'services[].{service:serviceName,desired:desiredCount,running:runningCount,taskDef:taskDefinition}' \
    --output table 2>/dev/null || log "services: none"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "usage: services.sh up|down|status" >&2; exit 1 ;;
esac
