FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies only — cached unless pyproject/lock change
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Now bring in the app code
COPY . .

EXPOSE 10000
CMD ["uv", "run", "main.py"]