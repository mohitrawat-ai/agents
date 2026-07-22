#!/bin/sh
# rca/entrypoint.sh — writes ~/.aws/config at container start (issue #9).
#
# aws_log.py defaults to `--profile hb-role`. In the container that profile
# must assume the partner role using the task role's credentials, so the
# config says credential_source = EcsContainer (#15; no static partner keys).
#
# The role ARN and ExternalId arrive as env vars, injected by the task
# definition from SSM (/rca/partner-aws/*). They are not baked into the
# image: the ExternalId is a SecureString, and an image is not a secret
# store. Tasks without partner access (poller, router) simply omit the
# vars and get no profile.

set -eu

if [ -n "${PARTNER_ROLE_ARN:-}" ] && [ -n "${PARTNER_EXTERNAL_ID:-}" ]; then
  mkdir -p "$HOME/.aws"
  cat > "$HOME/.aws/config" <<EOF
[profile hb-role]
role_arn = ${PARTNER_ROLE_ARN}
external_id = ${PARTNER_EXTERNAL_ID}
credential_source = EcsContainer
region = ap-south-1
EOF
fi

exec "$@"
