FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    POETRY_VERSION=2.3.2 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTEGBOT_API_CONFIG=/config/api.yaml

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

WORKDIR /app/api
COPY shared /app/shared
COPY api /app/api

RUN poetry install --no-interaction --no-ansi

CMD ["python", "-m", "pytegbot_api"]
