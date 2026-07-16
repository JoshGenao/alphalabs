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

# SRS-RES-001: the concrete JupyterLab research environment (was the Phase-1
# print-stub; operators may still substitute a hardened base image, but the
# template is now runnable end to end). Pinned to the version the embed was
# verified against (tests/e2e/test_research_embed.py: iframe render + kernel
# round-trip through the dashboard's /research/ proxy).
RUN pip install --no-cache-dir jupyterlab==4.6.1

# Non-root: notebooks execute arbitrary code; the account owns only its home.
# The SSD/NAS tiers are mounted read-only by compose (SRS-SEC-004) and linked
# into the home directory for discoverability in the Lab file browser.
RUN useradd --create-home --shell /usr/sbin/nologin researcher \
 && ln -s /ssd /home/researcher/ssd \
 && ln -s /nas /home/researcher/nas
USER researcher
WORKDIR /home/researcher

# Launch notes (each flag is deliberate):
# - base_url=/research/ — the dashboard runtime proxies the same-origin
#   /research/ prefix VERBATIM (no path rewriting), so Lab must serve under it
#   (IF-13: embedded in the dashboard, not a standalone endpoint).
# - ip=0.0.0.0 INSIDE this container only: it is confined to the internal
#   atp_research_net (no gateway — jupyter_isolation_check enforces the
#   network shape statically), so the reachable audience is exactly the
#   one-way research-proxy hop. The compose file publishes NO port for it.
# - token/password empty — the auth boundary is the network path (only the
#   research-proxy can reach this container, and only the loopback-bound
#   dashboard reaches the research-proxy). Injecting a token env here would
#   contradict the SRS-SEC-004 no-secrets stance this container is checked
#   against (x-atp-no-secrets merged first).
# - allow_remote_access — the Host header seen through the proxy chain is
#   phase1-jupyter:8888 (non-local), which Lab would otherwise refuse.
CMD ["python", "-m", "jupyter", "lab", \
     "--ServerApp.ip=0.0.0.0", \
     "--ServerApp.port=8888", \
     "--ServerApp.base_url=/research/", \
     "--ServerApp.allow_remote_access=True", \
     "--IdentityProvider.token=", \
     "--ServerApp.password=", \
     "--no-browser"]
