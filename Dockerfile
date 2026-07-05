# hansard-researcher pipeline image — CLI only; the data plane lives on a mounted
# volume (see compose.yaml). Dashboards are a separate npm project and are
# not part of this image.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
# dependency layer first so code edits don't re-resolve the lock
# (--extra api: fastapi+uvicorn for the Tier 2 search service — small; the
# heavy 'local' extra with torch is deliberately NOT installed)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra api
COPY README.md LICENSE ./
COPY src ./src
# --no-editable: install the package into the venv itself so the runtime
# stage needs only /app/.venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra api

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 1000 hansard \
    && mkdir /data && chown hansard:hansard /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    HANSARD_RESEARCHER_DATA_DIR=/data
USER hansard
VOLUME /data
ENTRYPOINT ["hansard-researcher"]
CMD ["sources"]
