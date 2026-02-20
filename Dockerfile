# ---- Builder stage ----
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only what's needed for the build
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

# Create venv and install all extras
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir '.[all]'

# Install Playwright Chromium browser
RUN playwright install chromium

# ---- Runtime stage ----
FROM python:3.12-slim

# Runtime system deps: tesseract for OCR, curl for healthcheck,
# shared libs required by Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    curl \
    # Playwright Chromium shared libs
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libx11-xcb1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js + CLI-based agent backends
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code @openai/codex && \
    npm cache clean --force && \
    rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy Playwright browsers from builder
COPY --from=builder /root/.cache/ms-playwright /home/pocketpaw/.cache/ms-playwright

# Create non-root user
RUN groupadd --system pocketpaw && \
    useradd --system --gid pocketpaw --create-home pocketpaw && \
    mkdir -p /home/pocketpaw/.pocketpaw /home/pocketpaw/workspace && \
    chown -R pocketpaw:pocketpaw /home/pocketpaw

USER pocketpaw
WORKDIR /home/pocketpaw

# Bind to 0.0.0.0 so the container port is reachable from the host
ENV POCKETPAW_WEB_HOST=0.0.0.0
ENV POCKETPAW_WEB_PORT=8888
# Disable localhost auth bypass — Docker bridge networking means requests
# arrive from 172.x.x.x, not 127.0.0.1, so the bypass would never trigger.
# Users authenticate with the access token instead.
ENV POCKETPAW_LOCALHOST_AUTH_BYPASS=false
# Agent-created files land here — bind-mount to access them on the host
ENV POCKETPAW_FILE_JAIL_PATH=/home/pocketpaw/workspace

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8888/ || exit 1

CMD ["pocketpaw"]
