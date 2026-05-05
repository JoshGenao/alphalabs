FROM python:3.12-slim

WORKDIR /workspace
COPY python ./python

ENV PYTHONPATH=/workspace/python

# The Phase 1 dashboard/API binds to loopback by default (SRS-SEC-002).
# This compile-only image validates the OpenAPI snapshot is present and
# loadable. Live HTTP serving lands with the dashboard implementation.
CMD ["python", "-c", "import json, os; from atp_api.openapi import render_snapshot; render_snapshot(); print('atp_api dashboard ready, BIND_HOST=' + os.environ.get('ATP_DASHBOARD_BIND_HOST', '127.0.0.1'))"]
