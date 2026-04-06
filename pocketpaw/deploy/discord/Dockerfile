# Full-featured Discord bot with all agent backends and tools (no browser)

# ---- Node.js stage ----
FROM node:22.14.0-slim AS node

RUN npm install -g @anthropic-ai/claude-code @openai/codex && \
    npm cache clean --force

# ---- Builder stage ----
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir '.[all]'

# ---- Runtime stage ----
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Copy Node.js + globally-installed CLI backends from the official node image
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js /usr/local/bin/claude && \
    ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user
RUN groupadd --system pocketpaw && \
    useradd --system --gid pocketpaw --create-home pocketpaw && \
    mkdir -p /home/pocketpaw/.pocketpaw /home/pocketpaw/workspace /home/pocketpaw/.claude && \
    chown -R pocketpaw:pocketpaw /home/pocketpaw

USER pocketpaw
WORKDIR /home/pocketpaw

# Claude Code requires ~/.claude.json to skip the onboarding/login prompt.
# Without this marker file, it treats every start as a fresh install.
RUN echo '{}' > /home/pocketpaw/.claude.json

ENV HOME=/home/pocketpaw
# Agent-created files land here — bind-mount to access them on the host
ENV POCKETPAW_FILE_JAIL_PATH=/home/pocketpaw/workspace

CMD ["pocketpaw", "--discord"]
