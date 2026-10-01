# syntax=docker/dockerfile:1
# openEduSuite Typst render service — stdlib HTTP wrapper (render-service/app.py)
# around the typst CLI, plus Liberation fonts for real-world documents.
#
# Build stages mirror the repo-root Dockerfile (xx cross-compile) so this image
# is self-contained and never races the separately published base image on ghcr.
FROM --platform=$BUILDPLATFORM tonistiigi/xx:1.9.0 AS xx
FROM --platform=$BUILDPLATFORM rust:1.98.0-alpine3.23 AS build

COPY --from=xx / /

RUN apk add --no-cache clang lld
COPY . /app
WORKDIR /app
RUN --mount=type=cache,target=/root/.cargo/git/db \
    --mount=type=cache,target=/root/.cargo/registry/cache \
    --mount=type=cache,target=/root/.cargo/registry/index \
    CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \
    cargo fetch

ARG TARGETPLATFORM

RUN xx-apk add --no-cache musl-dev openssl-dev openssl-libs-static
RUN --mount=type=cache,target=/root/.cargo/git/db \
    --mount=type=cache,target=/root/.cargo/registry/cache \
    --mount=type=cache,target=/root/.cargo/registry/index \
    OPENSSL_NO_PKG_CONFIG=1 OPENSSL_STATIC=1 \
    OPENSSL_DIR=$(xx-info is-cross && echo /$(xx-info)/usr/ || echo /usr) \
    xx-cargo build -p typst-cli --release && \
    cp target/$(xx-cargo --print-target-triple)/release/typst target/release/typst && \
    xx-verify target/release/typst

FROM python:3.12-alpine
ARG CREATED
ARG REVISION

RUN apk add --no-cache font-liberation

LABEL org.opencontainers.image.created=${CREATED}
LABEL org.opencontainers.image.description="openEduSuite Typst render service — HTTP API around the typst CLI"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.revision=${REVISION}
LABEL org.opencontainers.image.source="https://github.com/tobias-weiss-ai-xr/typst"
LABEL org.opencontainers.image.title="openEduSuite Typst Render Service"
LABEL org.opencontainers.image.vendor="openEduSuite"

COPY --from=build /app/target/release/typst /bin/typst
WORKDIR /app
COPY render-service/app.py render-service/test_app.py ./

ENV PYTHONDONTWRITEBYTECODE=1
USER 1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1

CMD ["python", "app.py"]
