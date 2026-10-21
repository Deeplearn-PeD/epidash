FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first (cached layer), then the app code
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY epidash ./epidash
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    EPIDASH_DB_PATH=/data/epidash.db

RUN useradd --system --create-home epidash \
    && mkdir -p /data \
    && chown -R epidash:epidash /app /data
USER epidash

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["uvicorn", "epidash.main:app", "--host", "0.0.0.0", "--port", "8000"]
