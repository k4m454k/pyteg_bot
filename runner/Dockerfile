FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 runner

WORKDIR /opt/runner
COPY runner/executor.py /opt/runner/executor.py

USER runner
ENTRYPOINT ["python", "/opt/runner/executor.py"]
