FROM rust:1.75-slim

ARG ATP_CRATE=atp-orchestrator
WORKDIR /workspace

# Cargo.lock is required by `cargo --locked`, and rust-toolchain.toml pins the toolchain the
# workspace + lock were resolved with (channel 1.95.0). Both MUST be copied before any cargo
# invocation — omitting Cargo.lock makes `--locked` fail immediately ("lock file needs to be
# updated"), and omitting rust-toolchain.toml silently builds under the base image's Rust instead
# of the pinned toolchain. This is the whole reason the phase1 Rust service images build.
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY crates ./crates

RUN cargo test --locked -p "${ATP_CRATE}" --lib

CMD ["cargo", "test", "--locked", "--workspace", "--lib"]
