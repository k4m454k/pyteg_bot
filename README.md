# PyTegBot

PyTegBot is a Telegram bot plus execution API for running short Python snippets inside isolated Docker containers.

It supports:

- private chat execution by sending plain Python code
- group chat execution via `/code`
- inline mode execution with result insertion into any chat
- queued job processing with polling, cancellation, and in-memory task storage

The project targets Python 3.12+ and uses Poetry for all Python components.

## Architecture

The repository contains four parts:

- `api/`: FastAPI service that accepts execution jobs, authenticates bot/API clients, queues tasks, exposes task status, and cancels running jobs
- `bot/`: aiogram-based Telegram bot
- `shared/`: shared Pydantic models and YAML config loading helpers
- `runner/`: the minimal Docker image used to execute untrusted Python code

## How It Works

1. A user sends Python code to the bot.
2. The bot creates a job through the API.
3. The API stores the job in memory and pushes the job ID into an async queue.
4. One of the execution workers picks the job up.
5. The API creates a separate Docker container from the runner image and passes the code through a base64-encoded environment variable.
6. The runner decodes the payload, compiles it, and executes it with `exec(...)`.
7. The API waits for container completion, reads stdout/stderr from Docker logs, stores the result, and removes the container.
8. The bot polls the API until it receives the final result and edits the Telegram message.

## Execution Isolation

Each Python snippet runs in its own disposable Docker container with these restrictions:

- no network access
- read-only root filesystem
- writable `tmpfs` only at `/tmp`
- all Linux capabilities dropped
- `no-new-privileges` enabled
- process limit (`pids_limit`)
- memory limit
- CPU limit
- hard execution timeout

Default limits are configured in `config/api.yaml`:

- max concurrent tasks: `2`
- max timeout: `20` seconds
- memory limit: `200m`
- CPU limit: `0.2 CPU`
- task TTL in memory: `30 minutes`

## Bot Behavior

- Private chats: send Python code directly.
- Groups and supergroups: the bot only reacts to messages that start with `/code`.
- Inline mode: type Python code after the bot username and choose the result entry.
- The bot is intentionally quiet and only responds when explicitly invoked.

Valid fenced code blocks include:

- ```` ```python ````
- ```` ```python3 ````
- ```` ```py ````
- ```` ```py3 ````

## API Endpoints

The API is bearer-token protected and exposes:

- `GET /health`
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/cancel`

Jobs are not stored in a database. They live in an in-memory store and are cleaned up automatically after the configured TTL.

## Configuration

Tracked configuration files live in `config/`:

- `config/api.yaml`
- `config/bot.yaml`

Local secret overrides should be created as:

- `config/api.local.yaml`
- `config/bot.local.yaml`

These local files are ignored by Git.

### Tokens You Must Fill In

Create `config/api.local.yaml` from `config/api.local.yaml.example` and set:

```yaml
server:
  auth_token: "replace-with-a-strong-shared-api-token"
```

Create `config/bot.local.yaml` from `config/bot.local.yaml.example` and set:

```yaml
telegram:
  bot_token: "123456789:replace-with-telegram-bot-token"

api:
  auth_token: "replace-with-the-same-api-auth-token"
```

Important:

- `server.auth_token` in the API and `api.auth_token` in the bot must be the same value.
- `telegram.bot_token` is the Telegram token from BotFather.

You can keep non-secret defaults in `config/api.yaml` and `config/bot.yaml`, and put only secrets into the `.local.yaml` files.

## Running with Docker Compose

Build all services:

```bash
docker compose build runner-base api bot
```

Start the stack:

```bash
docker compose up -d api bot
```

The API will listen on `http://localhost:8000` by default.

The API container requires access to the host Docker socket:

- `/var/run/docker.sock:/var/run/docker.sock`

Configuration is mounted from the host into `/config`. By default, Compose uses `./config`, but you can override that with `PYTEGBOT_CONFIG_DIR`.

Example:

```bash
PYTEGBOT_CONFIG_DIR=/opt/pytegbot/config docker compose up -d api bot
```

## Local Smoke Test

The repository includes `smoke_api.sh` for a basic API check.

Example:

```bash
PYTEGBOT_AUTH_TOKEN="your-api-token" ./smoke_api.sh
```

You can optionally point it at another host:

```bash
PYTEGBOT_BASE_URL="http://your-host:8000" \
PYTEGBOT_AUTH_TOKEN="your-api-token" \
./smoke_api.sh
```

## Deployment

This repository does not require a custom deploy script.

A typical deployment flow is:

1. Install Docker and Docker Compose on the target host.
2. Clone the repository on the target host.
3. Create `config/api.local.yaml` and `config/bot.local.yaml`.
4. Verify that the API container will have access to `/var/run/docker.sock`.
5. Build images:

```bash
docker compose build runner-base api bot
```

6. Start the services:

```bash
docker compose up -d api bot
```

7. Check health:

```bash
curl http://localhost:8000/health
```

If you want to keep runtime config outside the repository checkout, create a separate config directory on the host and start Compose with `PYTEGBOT_CONFIG_DIR=/path/to/config`.

## Development Notes

- All components use Poetry.
- The API and bot are asynchronous.
- The current implementation is intended for a single API instance.
- The task store is in-memory by design; no database is required for the initial version.
