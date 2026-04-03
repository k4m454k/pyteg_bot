# PyTegBot

PyTegBot — это Telegram-бот и API исполнения кода для запуска коротких Python-сниппетов внутри изолированных Docker-контейнеров.

Поддерживаются:

- выполнение в личных сообщениях при отправке обычного Python-кода
- выполнение в группах через `/code`
- выполнение в inline-режиме с вставкой результата в любой чат
- постановка задач в очередь, polling, отмена и in-memory хранение задач

Проект рассчитан на Python 3.12+ и использует Poetry для всех Python-компонентов.

## Architecture

Репозиторий состоит из четырёх частей:

- `api/`: сервис FastAPI, который принимает задачи, аутентифицирует клиентов, ставит задачи в очередь, отдаёт статус и умеет отменять выполнение
- `bot/`: Telegram-бот на aiogram
- `shared/`: общие Pydantic-модели и загрузка YAML-конфигов
- `runner/`: минимальный Docker-образ, в котором исполняется недоверенный Python-код

## How It Works

1. Пользователь отправляет Python-код боту.
2. Бот создаёт задачу через API.
3. API хранит задачу в памяти и кладёт её ID в асинхронную очередь.
4. Один из execution worker-ов забирает задачу.
5. API создаёт отдельный Docker-контейнер из runner-образа и выбирает внутренний способ передачи кода:
   - маленькие payload’ы передаются через base64-кодированную переменную окружения
   - большие payload’ы стримятся в runner через stdin контейнера
6. Runner загружает исходный код, компилирует его и исполняет через `exec(...)`.
7. Если код создаёт изображения, runner записывает manifest артефактов, а API копирует эти файлы из ещё живого контейнера.
8. API хранит stdout/stderr, статус задачи и извлечённые изображения ограниченное время, после чего удаляет контейнер.
9. Бот опрашивает API до получения финального результата, редактирует сообщение в Telegram, а затем отправляет изображения отдельными media-сообщениями.

## Execution Isolation

Каждый Python-сниппет исполняется в отдельном одноразовом Docker-контейнере со следующими ограничениями:

- нет доступа в сеть
- корневая файловая система только для чтения
- writable `tmpfs` только в `/tmp`
- сброшены все Linux capabilities
- включён `no-new-privileges`
- лимит на число процессов (`pids_limit`)
- лимит памяти
- лимит CPU
- жёсткий timeout исполнения

Ограничения по умолчанию задаются в `config/api.yaml`:

- максимум одновременно выполняемых задач: `2`
- максимальный timeout: `40` секунд
- лимит памяти: `400m`
- лимит CPU: `0.4 CPU`
- TTL задач в памяти: `30 минут`

## Preinstalled Executor Libraries

Runner-образ содержит небольшой набор offline-friendly библиотек:

- `numpy`: многомерные массивы и быстрые численные операции
- `sympy`: символьная математика, алгебра, упрощение выражений и решение уравнений
- `networkx`: графовые структуры и графовые алгоритмы
- `python-dateutil`: гибкий парсинг дат и относительная арифметика дат
- `pytz`: поддержка legacy-базы часовых поясов
- `PyYAML`: парсинг и сериализация YAML
- `orjson`: быстрый JSON
- `simplejson`: более гибкая работа с JSON, особенно с типами вроде `Decimal`
- `regex`: расширенный движок регулярных выражений с возможностями сверх стандартного `re`
- `tabulate`: форматированные текстовые таблицы для вывода в чат

Эти пакеты устанавливаются прямо в runner-образ, описанный в `runner/Dockerfile`.

В executor по умолчанию намеренно не включены HTTP-ориентированные библиотеки, потому что контейнеры исполнения не имеют исходящего доступа в сеть.

На слабых хостах, например Raspberry Pi, импорт более тяжёлых библиотек вроде `numpy` или `sympy` может заметно съедать часть execution timeout при cold start.

Для генерации изображений runner также включает:

- `Pillow`: создание и редактирование изображений
- `matplotlib`: графики и диаграммы
- `plotly`: API для построения графиков

`plotly` доступен, но проект сейчас возвращает файлы изображений, а не HTML-артефакты. Для практического использования в Telegram основными встроенными вариантами остаются `Pillow` и `matplotlib`.

## Image Output and Artifacts

PyTegBot умеет возвращать изображения, созданные Python-кодом, в обычных чатах с ботом.

Возврат image artifacts сейчас работает для:

- личных сообщений
- групп и супергрупп через `/code`

В inline-режиме это пока не поддерживается.

### How Image Discovery Works

Runner перед стартом пользовательского кода меняет текущую директорию на каталог вывода:

- директория вывода по умолчанию: `/tmp/pytegbot-out`

Это значит, что относительные пути работают естественно. Например, все варианты ниже валидны:

```python
img.save("plot.png")
plt.savefig("chart.png")
img.save("nested/result.png")
```

После завершения пользовательского кода runner рекурсивно сканирует output directory и записывает небольшой manifest с найденными файлами изображений.

Поддерживаемые форматы:

- `png`
- `jpg`
- `jpeg`
- `gif`
- `webp`

Рассматриваются только обычные файлы. Симлинки игнорируются.

### Matplotlib Behavior

`matplotlib` внутри execution-контейнера настроен на non-GUI backend `Agg`.

Поддерживаются два типичных сценария:

1. Явное сохранение:

```python
import matplotlib.pyplot as plt

plt.plot([1, 2, 3], [1, 4, 9])
plt.savefig("plot.png")
```

2. Auto-export открытых фигур:

```python
import matplotlib.pyplot as plt

plt.plot([1, 2, 3], [1, 4, 9])
print("figure created")
```

Если `matplotlib.pyplot` был импортирован и к моменту завершения пользовательского кода остаются открытые фигуры, runner пытается автоматически сохранить их как:

- `figure-1.png`
- `figure-2.png`
- и так далее

Это полезно, если пользователь забыл вызвать `savefig(...)` или использовал `plt.show()` в non-interactive окружении.

### How the API Stores Images

Когда runner сообщает, что артефакты готовы, API копирует их из контейнера и временно хранит у себя.

Пути хранения по умолчанию:

- output directory runner-а: `/tmp/pytegbot-out`
- storage артефактов API: `/tmp/pytegbot-artifacts`

Артефакты хранятся в API-контейнере в директории конкретной задачи:

- `/tmp/pytegbot-artifacts/<task_id>/`

Публичный ответ API на задачу включает только metadata артефактов:

- `artifact_id`
- `filename`
- `media_type`
- `size_bytes`

Содержимое файла скачивается через отдельный artifact endpoint.

### Telegram Delivery

Бот отправляет результат в две фазы:

1. Редактирует статусное сообщение в финальный текстовый результат.
2. Скачивает артефакты задачи из API и отправляет их отдельными Telegram media-сообщениями.

Текущее сопоставление с Telegram:

- PNG/JPEG -> photo
- GIF -> animation
- всё остальное -> document

### Retention and Cleanup

Артефакты живут столько же, сколько и сама задача.

По умолчанию:

- TTL задач/артефактов: `1800` секунд (`30 минут`)
- интервал cleanup: `60` секунд

Когда задача удаляется из in-memory store, директория её артефактов на стороне API тоже удаляется.

Практически важный нюанс:

- артефакты хранятся внутри файловой системы API-контейнера, а не на host bind mount
- если API-контейнер рестартовать или передеплоить, артефакты пропадут раньше TTL

### Artifact Limits

Лимиты по умолчанию задаются в `config/api.yaml`:

- максимум артефактов: `4`
- максимум на файл: `4 MiB`
- максимум суммарно на задачу: `8 MiB`

Файлы, превышающие эти лимиты, игнорируются и не будут возвращены боту.

### User Guidance

Для наиболее надёжного поведения лучше явно сохранять изображения:

```python
plt.savefig("plot.png")
```

или:

```python
from PIL import Image

img = Image.new("RGB", (200, 100), "white")
img.save("result.png")
```

Предпочтительны относительные пути, потому что runner уже переключает текущую директорию в output directory.

### Limitations

- Inline-режим пока не возвращает image artifacts.
- Image artifacts отправляются только после перехода задачи в terminal state.
- Тяжёлые библиотеки вроде `matplotlib` могут заметно съедать timeout на слабых хостах вроде Raspberry Pi.
- Если задача упадёт по timeout на поздней стадии finalization, текстовый вывод может уже быть, а изображения — нет.

## Bot Behavior

- Личные сообщения: можно просто отправлять Python-код.
- Группы и супергруппы: бот реагирует только на сообщения, начинающиеся с `/code`.
- Inline-режим: напишите Python-код после username бота и выберите результат из списка.
- Бот намеренно молчалив и отвечает только при явном вызове.

Допустимые fenced code blocks:

- ```` ```python ````
- ```` ```python3 ````
- ```` ```py ````
- ```` ```py3 ````

## Python File Uploads

Бот также умеет выполнять Python source file, отправленный как Telegram document.

Поддерживаемые сценарии:

- личные сообщения: отправьте один `.py` файл напрямую
- группы и супергруппы: отправьте один `.py` файл с caption, начинающимся с `/code`
- inline-режим: загрузка файлов не поддерживается

Правила валидации:

- должен быть отправлен ровно один файл
- имя файла должно заканчиваться на `.py`
- максимальный размер файла — `5 MiB`
- файл должен декодироваться как корректный текстовый исходник
- пустые файлы отклоняются

Если пользователь отправит media group с несколькими файлами, бот отклонит её и попросит прислать один `.py` файл.

Бот скачивает document, декодирует его с помощью определения encoding для Python source и затем отправляет уже декодированный исходный текст в API как обычную execution task. Внешний контракт API не меняется только потому, что пользователь загрузил файл.

Ответы Telegram для file submissions намеренно отличаются от текстовых:

- промежуточное статусное сообщение показывает имя принятого файла
- финальное сообщение показывает имя файла и результат исполнения
- исходный код файла не репостится обратно в чат

Это делает выполнение больших скриптов менее шумным и не засоряет чат длинным телом файла.

## Large Code Transport

Большие payload’ы кода обрабатываются внутри execution pipeline отдельно.

Зачем это нужно:

- переменные окружения Linux плохо подходят для передачи исходников размером в несколько мегабайт
- base64 добавляет overhead по размеру
- очень большие `argv/env` payload’ы упираются в ограничения ядра и запуска процесса

Поэтому PyTegBot использует гибридную стратегию внутренней передачи кода:

- если UTF-8 исходник не превышает `execution.max_env_code_bytes`, API использует путь через переменную окружения
- если исходник больше этого порога, API копирует исходник внутрь runner-контейнера как временный `.py` файл и затем запускает выполнение уже из этого файла

Порог по умолчанию:

- `execution.max_env_code_bytes: 97280`

Путь к временному файлу задаётся в конфиге:

- `execution.code_file_path`

Практические плюсы:

- большие `.py` файлы не зависят от oversized environment variables
- API не зависит от oversized environment variables даже для мегабайтных payload’ов
- маленькие сниппеты остаются быстрыми, потому что для них сохраняется env-based path

У upload phase есть отдельный короткий timeout:

- `execution.code_upload_timeout_seconds: 15`

Обычный timeout исполнения Python при этом остаётся отдельным:

- `execution.max_timeout_seconds: 40`

То есть доставка большого файла и исполнение кода считаются разными фазами. Медленная загрузка может быстро завершиться ошибкой, не съедая весь execution budget, а успешно загруженный код всё равно получает нормальный timeout runner-а.

## Local Testing

В репозитории есть `Makefile` для локальной проверки без деплоя на Raspberry Pi.

Доступные цели:

- `make build_test`: собирает `pytegbot-runner:local`, устанавливает test dependencies для API и переустанавливает `shared` в editable-режиме внутри API virtualenv
- `make test`: запускает текущий автоматизированный test suite
- `make lint`: запускает `poetry check` для `shared`, `api`, `bot` и затем `compileall` по исходникам

Полный локальный прогон:

```bash
make build_test test lint
```

Сейчас автоматические тесты покрывают в первую очередь API integration flow. Проверяются:

- `GET /health`
- bearer auth на `POST /v1/tasks`
- выполнение маленького Python-кода
- выполнение большого Python-кода
- отмена задачи
- возврат и скачивание image artifact

Для локальных тестов нужен работающий Docker daemon, потому что test suite поднимает настоящий runner image и проверяет реальное выполнение кода в Docker.

Если Docker daemon доступен не через стандартный сокет, можно явно задать base URL:

```bash
PYTEGBOT_TEST_DOCKER_BASE_URL="unix:///path/to/docker.sock" make test
```

## API Endpoints

API защищён bearer token и предоставляет:

- `GET /health`
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/cancel`
- `GET /v1/tasks/{task_id}/artifacts/{artifact_id}`

Задачи не сохраняются в базе данных. Они живут в in-memory store и автоматически удаляются после настроенного TTL.

## Configuration

Tracked-конфиги лежат в `config/`:

- `config/api.yaml`
- `config/bot.yaml`

Локальные overrides для секретов нужно создавать как:

- `config/api.local.yaml`
- `config/bot.local.yaml`

Эти локальные файлы игнорируются Git.

### Tokens You Must Fill In

Создайте `config/api.local.yaml` на основе `config/api.local.yaml.example` и задайте:

```yaml
server:
  auth_token: "replace-with-a-strong-shared-api-token"
```

Создайте `config/bot.local.yaml` на основе `config/bot.local.yaml.example` и задайте:

```yaml
telegram:
  bot_token: "123456789:replace-with-telegram-bot-token"

api:
  auth_token: "replace-with-the-same-api-auth-token"
```

Важно:

- `server.auth_token` в API и `api.auth_token` в боте должны быть одинаковыми
- `telegram.bot_token` — это Telegram token из BotFather

Можно хранить не-секретные значения в `config/api.yaml` и `config/bot.yaml`, а секреты — только в `.local.yaml`.

## Running with Docker Compose

Соберите все сервисы:

```bash
docker compose build runner-base api bot
```

Запустите стек:

```bash
docker compose up -d api bot
```

По умолчанию API будет слушать на `http://localhost:8000`.

API-контейнеру нужен доступ к Docker socket хоста:

- `/var/run/docker.sock:/var/run/docker.sock`

Конфигурация монтируется с хоста в `/config`. По умолчанию Compose использует `./config`, но это можно переопределить через `PYTEGBOT_CONFIG_DIR`.

Пример:

```bash
PYTEGBOT_CONFIG_DIR=/opt/pytegbot/config docker compose up -d api bot
```

## Local Smoke Test

В репозитории есть `smoke_api.sh` для базовой проверки API.

Пример:

```bash
PYTEGBOT_AUTH_TOKEN="your-api-token" ./smoke_api.sh
```

Можно при необходимости направить его на другой хост:

```bash
PYTEGBOT_BASE_URL="http://your-host:8000" \
PYTEGBOT_AUTH_TOKEN="your-api-token" \
./smoke_api.sh
```

## Deployment

Для этого репозитория не требуется отдельный deploy script.

Типичный сценарий деплоя:

1. Установить Docker и Docker Compose на целевой хост.
2. Склонировать репозиторий на целевой хост.
3. Создать `config/api.local.yaml` и `config/bot.local.yaml`.
4. Проверить, что API-контейнер получит доступ к `/var/run/docker.sock`.
5. Собрать образы:

```bash
docker compose build runner-base api bot
```

6. Запустить сервисы:

```bash
docker compose up -d api bot
```

7. Проверить health:

```bash
curl http://localhost:8000/health
```

Если хотите хранить runtime-конфиг вне checkout репозитория, создайте отдельную config directory на хосте и запускайте Compose с `PYTEGBOT_CONFIG_DIR=/path/to/config`.

## Development Notes

- Все компоненты используют Poetry.
- API и бот полностью асинхронные.
- Текущая реализация рассчитана на один экземпляр API.
- Task store намеренно in-memory; база данных на первом этапе не требуется.
