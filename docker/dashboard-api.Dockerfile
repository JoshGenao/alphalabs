FROM python:3.12-slim

WORKDIR /workspace

# `python -m atp_dashboard` is NOT stdlib-only: importing any provider pulls
# atp_readiness -> atp_config.vault -> cryptography (Fernet, SRS-SEC-001). Take
# ONLY that requirement, and take it FROM requirements.txt rather than repeating
# a version here — its floor is security-driven (>=48.0.1 clears four advisories)
# and a second copy of the pin would silently drift below it. The rest of
# requirements.txt (pandas/pandas-ta/TA-Lib/matplotlib) is deliberately NOT
# installed: nothing the dashboard imports needs it, and TA-Lib would drag in a
# C library this image has no reason to carry.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir "$(grep -E '^cryptography' requirements.txt)"

COPY python ./python
# The runtime resolves its own contract at import time — `atp_runtime.contract`
# reads `<repo root>/architecture/runtime_services.json`, and `atp_runtime.ROOT`
# is `parents[2]` of the package, i.e. this WORKDIR. Without it BOTH services
# built from this image (phase1-dashboard-api and phase1-research-proxy, which
# overrides `command`) die at startup with FileNotFoundError — the reason the
# research chain had never actually run in a container before SRS-RES-001's
# deploy leg was exercised.
COPY architecture ./architecture

ENV PYTHONPATH=/workspace/python

# The Phase 1 dashboard/API binds to loopback by default (SRS-SEC-002).
# This compile-only image validates the OpenAPI snapshot is present and
# loadable. Live HTTP serving lands with the dashboard implementation.
CMD ["python", "-c", "import json, os; from atp_api.openapi import render_snapshot; render_snapshot(); print('atp_api dashboard ready, BIND_HOST=' + os.environ.get('ATP_DASHBOARD_BIND_HOST', '127.0.0.1'))"]
