FROM python:3.12-slim

# SRS-SDK-006 / SyRS C-9: TA-Lib C library is a mandatory native dependency
# for the Python TA-Lib wrapper. We install C version 0.6.4 from the
# upstream GitHub release because the Python wrapper pinned in
# requirements.txt (TA-Lib>=0.6.0) is built against the 0.6.x ABI and
# fails to link against the older 0.4.0 C library. build-essential +
# wget are purged after install to keep the layer slim.
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

CMD ["python", "-c", "from atp_strategy import Strategy; print('strategy API ready')"]
