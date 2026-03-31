FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    POETRY_VERSION=2.3.2 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTEGBOT_BOT_CONFIG=/config/bot.yaml

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

WORKDIR /app/bot
COPY shared /app/shared
COPY bot /app/bot

RUN poetry install --no-interaction --no-ansi

CMD ["python", "-m", "pytegbot_bot"]
