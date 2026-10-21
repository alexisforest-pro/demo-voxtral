FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.12-slim AS builder

COPY --from=uv /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY . .
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --uid 1001 --create-home worker && chown -R 1001 /app
USER 1001

CMD ["/app/.venv/bin/python", "-m", "workers.worker"]
