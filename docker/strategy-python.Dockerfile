FROM python:3.12-slim

WORKDIR /workspace
COPY python ./python

ENV PYTHONPATH=/workspace/python

CMD ["python", "-c", "from atp_strategy import Strategy; print('strategy API ready')"]
