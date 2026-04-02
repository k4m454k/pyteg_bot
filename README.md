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
5. The API creates a separate Docker container from the runner image and chooses an internal code transport:
   - small payloads go through a base64-encoded environment variable
   - larger payloads are streamed into the runner through container stdin
6. The runner loads the source code, compiles it, and executes it with `exec(...)`.
7. If the code produces image files, the runner writes an artifact manifest and the API copies the files out of the still-running container.
8. The API stores stdout/stderr, task status, and any extracted image artifacts for a limited time, then removes the container.
9. The bot polls the API until it receives the final result, edits the Telegram message, and then sends image artifacts as separate Telegram media messages.

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
- max timeout: `40` seconds
- memory limit: `400m`
- CPU limit: `0.4 CPU`
- task TTL in memory: `30 minutes`

## Preinstalled Executor Libraries

The runner image ships with a small offline-friendly standard toolkit:

- `numpy`: n-dimensional arrays and fast numerical operations
- `sympy`: symbolic math, algebra, simplification, and equation solving
- `networkx`: graph structures and graph algorithms
- `python-dateutil`: flexible date parsing and relative date arithmetic
- `pytz`: legacy timezone database support
- `PyYAML`: YAML parsing and serialization
- `orjson`: fast JSON parsing and serialization
- `simplejson`: flexible JSON handling, especially useful with types such as `Decimal`
- `regex`: an extended regular expression engine with features beyond the standard `re` module
- `tabulate`: formatted plain-text tables for chat-friendly output

These packages are installed directly into the runner image defined in `runner/Dockerfile`.

The executor intentionally does not bundle HTTP-focused libraries as a default feature set, because execution containers have no outbound network access.

On low-power hosts such as a Raspberry Pi, importing heavier libraries like `numpy` or `sympy` can take a noticeable part of the execution timeout during a cold start.

For image-oriented code, the runner also includes:

- `Pillow`: image generation and editing
- `matplotlib`: charts and plots
- `plotly`: chart construction APIs

`plotly` is available, but this project currently returns image files, not HTML artifacts. For practical Telegram output, `Pillow` and `matplotlib` are the main built-in options.

## Image Output and Artifacts

PyTegBot can return image files produced by Python code in normal bot chats.

Image artifact delivery currently applies to:

- private chats
- groups and supergroups via `/code`

It does not currently apply to inline mode.

### How Image Discovery Works

The runner sets the current working directory to the configured output directory before user code starts:

- default output directory: `/tmp/pytegbot-out`

That means relative paths work naturally. For example, all of these are valid:

```python
img.save("plot.png")
plt.savefig("chart.png")
img.save("nested/result.png")
```

The runner scans the output directory recursively after the user code finishes and writes a small manifest describing discovered image files.

Supported file types are:

- `png`
- `jpg`
- `jpeg`
- `gif`
- `webp`

Only regular files are considered. Symlinks are ignored.

### Matplotlib Behavior

`matplotlib` is configured to use the non-GUI `Agg` backend inside the execution container.

Two common patterns are supported:

1. Explicit save:

```python
import matplotlib.pyplot as plt

plt.plot([1, 2, 3], [1, 4, 9])
plt.savefig("plot.png")
```

2. Auto-export of still-open figures:

```python
import matplotlib.pyplot as plt

plt.plot([1, 2, 3], [1, 4, 9])
print("figure created")
```

If `matplotlib.pyplot` was imported and there are still open figures when user code ends, the runner attempts to export them automatically as:

- `figure-1.png`
- `figure-2.png`
- and so on

This is useful when a user forgets to call `savefig(...)`, or when they use `plt.show()` in a non-interactive environment.

### How the API Stores Images

When the runner announces that artifacts are ready, the API copies them out of the container and stores them temporarily on the API side.

Default storage paths:

- runner output directory: `/tmp/pytegbot-out`
- API artifact storage: `/tmp/pytegbot-artifacts`

Artifacts are stored under a per-task directory in the API container:

- `/tmp/pytegbot-artifacts/<task_id>/`

The public task response includes only artifact metadata:

- `artifact_id`
- `filename`
- `media_type`
- `size_bytes`

The actual file content is downloaded through the artifact endpoint.

### Telegram Delivery

The bot sends results in two phases:

1. It edits the status message into the final text result.
2. It downloads any task artifacts from the API and sends them as separate Telegram media messages.

Telegram mapping is currently:

- PNG/JPEG -> photo
- GIF -> animation
- everything else -> document

### Retention and Cleanup

Artifacts live for the same TTL as task results.

By default:

- task/artifact TTL: `1800` seconds (`30 minutes`)
- cleanup interval: `60` seconds

Once a task expires from the in-memory store, its artifact directory is deleted from API storage as well.

Important practical note:

- artifacts are stored inside the API container filesystem, not on a host bind mount
- if the API container is restarted or redeployed, stored artifacts are lost even if the TTL has not expired yet

### Artifact Limits

Default artifact limits are configured in `config/api.yaml`:

- max artifact count: `4`
- max artifact size per file: `4 MiB`
- max total artifact size per task: `8 MiB`

Files that exceed these limits are ignored and will not be returned to the bot.

### User Guidance

For the most reliable behavior, tell users to save images explicitly:

```python
plt.savefig("plot.png")
```

or:

```python
from PIL import Image

img = Image.new("RGB", (200, 100), "white")
img.save("result.png")
```

Relative paths are preferred because the runner already changes into the output directory.

### Limitations

- Inline mode does not currently return image artifacts.
- Image artifacts are delivered only after the task reaches a terminal state.
- Heavy libraries such as `matplotlib` can consume a large part of the timeout budget on small hosts such as Raspberry Pi.
- If a task times out during late-stage finalization, text output may still be present while an image is missing.

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

## Python File Uploads

The bot can also execute a Python source file sent as a Telegram document.

Supported chat flows:

- Private chats: send one `.py` file directly.
- Groups and supergroups: send one `.py` file with a caption that starts with `/code`.
- Inline mode: file uploads are not supported.

Validation rules:

- exactly one file must be sent
- the filename must end with `.py`
- maximum file size is `5 MiB`
- the file must decode as valid text source
- empty files are rejected

If a media group contains multiple files, the bot rejects it and asks for a single `.py` file.

The bot downloads the document, decodes it using Python source encoding detection, and then sends the decoded source text to the API as a normal execution task. The external API contract does not change just because the user uploaded a file.

Telegram responses for file submissions are intentionally different from text submissions:

- the intermediate status message shows the accepted filename
- the final result message shows the filename and execution result
- the original source code is not echoed back into the chat

This makes large script execution less noisy in chats and avoids reposting a long file body into Telegram.

## Large Code Transport

Large code payloads are handled differently inside the execution pipeline.

Why this exists:

- Linux environment variables are a poor transport for multi-megabyte source files
- base64 adds size overhead
- very large `argv/env` payloads run into kernel and process startup limits

PyTegBot therefore uses a hybrid transport strategy inside the API:

- if the UTF-8 source is at or below `execution.max_env_code_bytes`, the API uses the existing environment-variable path
- if the source is larger than that threshold, the API starts the runner container with stdin open and streams the raw source code directly into container stdin

Default threshold:

- `execution.max_env_code_bytes: 97280`

For the stdin path, the runner switches into a dedicated input mode and reads the full source from stdin before executing user code.

This has two practical advantages:

- large `.py` uploads do not depend on oversized environment variables
- the API no longer needs to split big files into many small helper uploads

The upload phase has its own short timeout:

- `execution.code_upload_timeout_seconds: 15`

The normal Python execution timeout remains separate:

- `execution.max_timeout_seconds: 40`

So large file delivery and code execution are treated as different phases. A slow upload can fail quickly without consuming the full execution budget, while successfully uploaded code still gets the normal runner timeout.

## API Endpoints

The API is bearer-token protected and exposes:

- `GET /health`
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/cancel`
- `GET /v1/tasks/{task_id}/artifacts/{artifact_id}`

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
