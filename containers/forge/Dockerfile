# Stage 1: Build the next-generation Niuu web UI
FROM node:24-alpine@sha256:a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd AS web

WORKDIR /app

RUN corepack enable

# Copy workspace manifests for dependency resolution
COPY web-next/package.json ./
COPY web-next/pnpm-lock.yaml ./
COPY web-next/pnpm-workspace.yaml ./
COPY web-next/tsconfig.base.json ./
COPY web-next/tsconfig.json ./
COPY web-next/build ./build

# Copy the app and workspace packages
COPY web-next/apps ./apps
COPY web-next/packages ./packages

ARG APP_VERSION=dev
ENV NODE_OPTIONS="--max-old-space-size=4096"
ENV VITE_APP_VERSION=${APP_VERSION}

RUN pnpm install --frozen-lockfile
RUN pnpm build


FROM python:3.14-slim-trixie@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS builder

ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    UV_VERSION="0.11.10"; \
    case "${TARGETARCH}" in \
      amd64) UV_ARCHIVE="uv-x86_64-unknown-linux-gnu.tar.gz"; UV_SHA256="077e1a0777bcf516e02f4ef245e269c8d1baa780438e4c50e09c5c997f85538a" ;; \
      arm64) UV_ARCHIVE="uv-aarch64-unknown-linux-gnu.tar.gz"; UV_SHA256="91d5f4583539640765662ef86edcf3bf4db07439b622c7bed50c961240162046" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}" -o "/tmp/${UV_ARCHIVE}"; \
    echo "${UV_SHA256}  /tmp/${UV_ARCHIVE}" | sha256sum -c -; \
    tar -xzf "/tmp/${UV_ARCHIVE}" -C /tmp; \
    install -m 0755 "/tmp/${UV_ARCHIVE%.tar.gz}/uv" /usr/local/bin/uv; \
    rm -rf "/tmp/${UV_ARCHIVE}" "/tmp/${UV_ARCHIVE%.tar.gz}"

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock README.md /app/
COPY src/ /app/src/
COPY scripts/build_manifest.py /app/scripts/build_manifest.py
ARG BUILD_REVISION
ARG BUILD_REF=forge/ux-improvement
ARG APP_VERSION=forge-dev
RUN python scripts/build_manifest.py --revision "$BUILD_REVISION" --ref "$BUILD_REF" --version "$APP_VERSION"
RUN uv sync --frozen --no-dev --no-editable --extra k8s --extra nats --extra openshell --extra otel --extra tui \
    && mkdir -p /dist-root/data/mimir

FROM python:3.14-slim-trixie@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6


ARG BUILD_REVISION
ARG BUILD_REF=forge/ux-improvement
ARG APP_VERSION=forge-dev
LABEL org.opencontainers.image.title="Niuu Forge"
LABEL org.opencontainers.image.revision="$BUILD_REVISION"
LABEL org.opencontainers.image.version="$APP_VERSION"
LABEL org.opencontainers.image.ref.name="$BUILD_REF"
LABEL org.opencontainers.image.description="Niuu — unified Python control plane services"
LABEL org.opencontainers.image.vendor="Niuu Labs"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/xteo/niuu"

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv
COPY --from=builder --chown=65532:65532 /dist-root/data /data
COPY --from=web --chown=65532:65532 /app/apps/niuu/dist /opt/venv/lib/python3.14/site-packages/cli/web/dist
ENV NIUU_DATABASE__MODE=external
ENV NIUU_DATABASE_MODE=external
ENV NIUU_SERVER__HOST=0.0.0.0
ENV NIUU_SERVER__PORT=8080
ENV NIUU_CONFIG=/etc/niuu/config.yaml
ENV HOME=/data

USER 65532:65532

WORKDIR /app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

ENTRYPOINT ["/opt/venv/bin/python", "-m", "uvicorn", "niuu.container:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
