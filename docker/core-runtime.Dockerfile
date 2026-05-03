FROM rust:1.75-slim

ARG ATP_CRATE=atp-orchestrator
WORKDIR /workspace

COPY Cargo.toml ./
COPY crates ./crates

RUN cargo test --locked -p "${ATP_CRATE}" --lib

CMD ["cargo", "test", "--locked", "--workspace", "--lib"]
