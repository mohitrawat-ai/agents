# rca/Dockerfile — the investigation task image (issue #9, unit 3).
#
# One image, grown as tasks land: today it runs investigator/run.py; the
# poller (#10) and router (#11) add their code with a COPY line when they
# ship. COPY is an allowlist — .env can never enter the image, which is
# #6's "no .env in the image" box. .dockerignore backs that up.
#
# No Node. claude_agent_sdk wheels ship a native `claude` CLI binary
# (`_bundled/claude`, per platform), and the SDK prefers it over PATH —
# confirmed against the hosting doc and the 0.2.122 wheel, 2026-07-22.
# The CLI version rides the SDK pin in uv.lock. Other pins match the
# laptop: uv 0.6.8, Python 3.12.

FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 — aws_log.py shells out to `aws`.
RUN arch=$(dpkg --print-architecture) \
    && case "$arch" in \
         amd64) a=x86_64 ;; \
         arm64) a=aarch64 ;; \
         *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${a}.zip" \
         -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

COPY --from=ghcr.io/astral-sh/uv:0.6.8 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Code layers last: an edit to a tool rebuilds only these.
COPY tools/ tools/
COPY investigator/ investigator/
COPY poller/ poller/
COPY slackbot/ slackbot/
COPY service/ service/
COPY qa/ qa/
COPY entrypoint.sh ./
RUN chmod 0755 entrypoint.sh

# Non-root: /app stays root-owned read-only; the run's only writable
# surface is /tmp (workdir + CLAUDE_CONFIG_DIR, run.py owns both).
RUN useradd --create-home --uid 1000 rca
USER rca

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "investigator/run.py"]
