FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential python3-dev \
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

RUN pip install --no-cache-dir .
RUN python -m playwright install --with-deps chromium

ENV PYTHONUNBUFFERED=1

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["python", "-m", "fscout.mcp_server", "--sse", "--host", "0.0.0.0"]
