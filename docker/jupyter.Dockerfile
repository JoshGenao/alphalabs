FROM python:3.12-slim

# SRS-SDK-006 / SyRS C-9: TA-Lib C library is a mandatory native dependency
# for the Python TA-Lib wrapper (the Jupyter research environment per
# SRS-RES-002 line 209 computes pandas-ta / TA-Lib indicators in
# notebooks, so the image needs the same C library 0.6.4 on the loader
# path as docker/strategy-python.Dockerfile). build-essential + wget are
# purged after install to keep the layer slim.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && wget -q https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz \
 && tar -xzf ta-lib-0.6.4-src.tar.gz \
 && cd ta-lib-0.6.4 \
 && ./configure --prefix=/usr/local \
 && make \
 && make install \
 && ldconfig \
 && cd / \
 && rm -rf /tmp/ta-lib-0.6.4 /tmp/ta-lib-0.6.4-src.tar.gz \
 && apt-get purge -y build-essential wget \
 && apt-get autoremove -y

WORKDIR /workspace
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY python ./python

ENV PYTHONPATH=/workspace/python
# SRS-SDK-006: disable numba JIT (irrelevant to SDK code paths; avoids
# import-time JIT failures on edge interpreter / LLVM combinations).
ENV NUMBA_DISABLE_JIT=1

# Phase 1 placeholder. The production Jupyter image installs JupyterLab
# and is launched read-only against the SSD/NAS data mounts per
# SRS-RES-001 and SRS-SEC-004. Operators supply the JupyterLab base
# image; this stub keeps `docker compose --profile phase1 config` valid
# without pulling JupyterLab during architecture-check builds.
CMD ["python", "-c", "import atp_strategy; print('jupyter research stub ready (SRS-RES-001), strategy api version exposed for notebooks')"]
