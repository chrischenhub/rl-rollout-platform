FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin

# Harbor is installed as its own tool so its dependencies never clash with ours.
# Pinned so platform and local `harbor run` results are comparable.
RUN uv tool install --with 'daytona==0.227.0' 'harbor[daytona]==0.25.0'

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY rollout ./rollout
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

CMD ["rollout", "api", "--host", "0.0.0.0", "--port", "8000"]
