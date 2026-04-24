# Multi-stage build for Agent Platform
# Stage 1: build the Rust binary
FROM rust:1.90-slim-bookworm AS builder

WORKDIR /build

# Cache dependencies
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo "fn main() {}" > src/main.rs && \
    cargo build --release && \
    rm -rf src target/release/deps/agent*

# Build actual sources
COPY src ./src
RUN cargo build --release

# Stage 2: runtime — Python3 for plugin modules, plus the binary
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -u 10001 -r -s /bin/false agent

WORKDIR /app
COPY --from=builder /build/target/release/agent /usr/local/bin/agent
COPY modules /app/modules
RUN chown -R agent:agent /app

USER agent

# Data directory should be mounted as a volume.
VOLUME ["/app/agent-data"]
EXPOSE 8090

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/agent", "/app/agent-data"]
