FROM debian:stable-slim

# Phase 1 placeholder for the operator-supplied headless IB Gateway image
# (SRS-EXE-006). Real IB Gateway distribution requires accepting
# Interactive Brokers' license at deployment time, so this Dockerfile is
# a stub that prints the configured endpoints and sleeps. The
# Strategy Orchestrator and adapters connect to the configured host and
# port; replace this image with the licensed IB Gateway container in
# production.
CMD ["sh", "-c", "echo \"IB Gateway placeholder: host=${ATP_IB_HOST:-127.0.0.1} live_port=${ATP_IB_LIVE_PORT:-4001} paper_port=${ATP_IB_PAPER_PORT:-4002}\" && sleep 3600"]
