FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable


FROM python:3.14-slim

RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/.venv /app/.venv

# No default on purpose: a build that was not told its commit reports "unknown".
ARG SLOT_BUILD_SHA
ENV PATH="/app/.venv/bin:$PATH" \
    SLOT_BUILD_SHA=${SLOT_BUILD_SHA}

USER app
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "booking.app:app", "--host", "0.0.0.0", "--port", "8000"]
