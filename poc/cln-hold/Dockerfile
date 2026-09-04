# CLN + BoltzExchange/hold plugin
# Build takes ~5-10 min (Rust compile); Docker layer caching makes rebuilds cheap.

FROM ubuntu:22.04 AS build
RUN apt-get update && apt-get -y install \
    gcc git curl ca-certificates protobuf-compiler libsqlite3-dev libpq-dev pkg-config
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN git clone --depth 1 https://github.com/BoltzExchange/hold /hold
WORKDIR /hold
RUN cargo build --release

# NOTE: pin this tag to the CLN version you tested with
FROM elementsproject/lightningd:latest
COPY --from=build /hold/target/release/hold /usr/local/bin/hold
RUN apt-get update && apt-get install -y --no-install-recommends libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*
