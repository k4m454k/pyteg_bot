# PyTegBot

PyTegBot is a small monorepo with two Python 3.12+ services:

- `api`: FastAPI service that accepts execution jobs, queues them, runs Python code inside constrained Docker containers, exposes status polling and cancellation.
- `bot`: aiogram Telegram bot that submits code to the API in chat and inline modes.

The bot supports:

- plain Python code in private chats
- `/code` in groups and supergroups
- inline execution for text snippets
- `.py` file uploads in private chats and groups
- image artifacts produced by user code in normal chat mode

## Layout

- `api/`: FastAPI service
- `bot/`: aiogram bot
- `shared/`: shared Pydantic models and config helpers
- `runner/`: prebuilt Docker image used for isolated Python execution
- `config/`: YAML configs mounted into containers from the host

## Quick Start

1. Fill in `config/bot.yaml` with the Telegram bot token.
2. Or better, put the Telegram bot token into `config/bot.local.yaml` so it survives config changes.
3. Replace the shared API auth token in both `config/api.yaml` and `config/bot.yaml` if you rotate it.
4. Build the runner image first:

```bash
docker compose build runner-base api bot
```

5. Start the stack:

```bash
docker compose up api bot
```

The API will be available on `http://localhost:8000`.

## Local Testing

Local tests use your local Docker daemon and the same runner image that production uses.

Available targets:

- `make build_test`: build `pytegbot-runner:local`, install API test dependencies, and reinstall `shared` in editable mode for the API virtualenv
- `make test`: run the current automated test suite
- `make lint`: run `poetry check` for all Python packages and `compileall` for the source tree

Run the full local verification flow:

```bash
make build_test test lint
```

Current automated coverage is API-focused. The integration suite verifies:

- `GET /health`
- bearer auth on `POST /v1/tasks`
- small code execution
- large code execution
- task cancellation
- image artifact download

If Docker is exposed through a non-default socket or context, you can override the test daemon explicitly:

```bash
PYTEGBOT_TEST_DOCKER_BASE_URL="unix:///path/to/docker.sock" make test
```

## Large Code Transport

PyTegBot uses two internal delivery modes when the API prepares code for the runner container:

- small code payloads are passed through a base64-encoded environment variable
- larger payloads are copied into the container as a temporary source file and then executed from that file

This avoids Linux `argv/env` size limits for large submissions while keeping small snippets fast.

Related settings live in `config/api.yaml`:

- `execution.max_env_code_bytes`
- `execution.code_upload_timeout_seconds`
- `execution.code_file_path`

## Notes

- Tasks are kept only in memory and are auto-removed after 30 minutes.
- The API limits concurrent executions with a worker queue.
- Code runs in a separate Docker image with memory, CPU and network restrictions.
- Clients may request a shorter execution timeout per task, but the API caps it by the configured maximum.
- Settings are loaded from `config/*.yaml` and may be overridden by `config/*.local.yaml`.
- The current implementation is designed for a single API instance.
