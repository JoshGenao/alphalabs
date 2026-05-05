FROM python:3.12-slim

WORKDIR /workspace
COPY python ./python

ENV PYTHONPATH=/workspace/python

# Phase 1 placeholder. The production Jupyter image installs JupyterLab
# and is launched read-only against the SSD/NAS data mounts per
# SRS-RES-001 and SRS-SEC-004. Operators supply the JupyterLab base
# image; this stub keeps `docker compose --profile phase1 config` valid
# without pulling JupyterLab during architecture-check builds.
CMD ["python", "-c", "import atp_strategy; print('jupyter research stub ready (SRS-RES-001), strategy api version exposed for notebooks')"]
