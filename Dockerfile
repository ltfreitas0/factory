# Factory control plane — FastAPI (uvicorn) as a Cloudflare Container.
#
# Build context is the REPO ROOT: routing/wrangler.jsonc points
# `"image": "../Dockerfile"` and wrangler defaults the build context to the
# Dockerfile's directory, so `COPY pyproject.toml uv.lock ./` resolves against
# the repo root (the routing dir is only the Worker + deploy config).
#
# Secrets are NOT baked in. Runtime config arrives as container envVars
# (wrangler.jsonc `vars` + `wrangler secret put`), forwarded by the routing
# Worker when it starts the singleton instance. See routing/wrangler.jsonc.

FROM python:3.12-slim

# uv (locked dep manager), git (repo clones in factory/project.py), curl (ops).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.local/bin:${PATH}" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Locked dependencies first so the big layer caches unless
#    pyproject.toml/uv.lock change. `--no-install-project` because factory/
#    is not copied yet; the second sync below installs the project itself.
#    `--extra dev` pulls pytest + httpx so the image can self-test
#    (image size is not the gate for this slice).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra dev --no-install-project

# 2) Application + tests, then install the project into the existing venv.
COPY factory/ ./factory/
COPY tests/ ./tests/
RUN uv sync --frozen --extra dev

EXPOSE 8051

# factory.api:main -> uvicorn on $FACTORY_API_HOST:$FACTORY_API_PORT (8051).
# The container MUST run with FACTORY_API_HOST=0.0.0.0 (set via container
# envVars), or uvicorn binds loopback and the platform's port probe never
# sees the app.
CMD ["uv", "run", "factory-api"]
