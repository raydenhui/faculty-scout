FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential python3-dev util-linux \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY schema.json config.yaml ./

RUN pip install --no-cache-dir fastmcp \
    && python -c "from mcp.server.fastmcp import FastMCP; print('MCP OK')"

RUN pip install --no-cache-dir ".[mcp,api]"

# Install Playwright browsers to a shared path so the non-root appuser can use them.
# Default installs to /root/.cache which appuser cannot read.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright && \
    python -m playwright install --with-deps chromium

ENV PYTHONUNBUFFERED=1

RUN useradd --create-home appuser \
    && mkdir -p /ms-playwright \
    && chown -R appuser:appuser /ms-playwright

# Copy entrypoint (runs as root, fixes volume ownership, then drops to appuser)
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

EXPOSE 8000
EXPOSE 8080

# Default: REST API server. Override CMD to run the MCP server instead.
CMD ["python", "-m", "fscout.rest_api", "--host", "0.0.0.0", "--port", "8000"]
