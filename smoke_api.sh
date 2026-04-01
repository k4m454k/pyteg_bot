#!/usr/bin/env sh
set -eu

BASE_URL="${PYTEGBOT_BASE_URL:-http://localhost:8000}"
AUTH_TOKEN="${PYTEGBOT_AUTH_TOKEN:-replace-with-api-auth-token}"
POLL_INTERVAL="${PYTEGBOT_POLL_INTERVAL:-1}"
MAX_POLLS="${PYTEGBOT_MAX_POLLS:-30}"
TASK_TIMEOUT="${PYTEGBOT_TASK_TIMEOUT:-10}"

CODE_FILE="${1:-}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT INT TERM

HEALTH_BODY="${TMP_DIR}/health.json"
CREATE_BODY="${TMP_DIR}/create.json"
TASK_BODY="${TMP_DIR}/task.json"

if [ -n "${CODE_FILE}" ]; then
  CODE="$(cat "${CODE_FILE}")"
else
  CODE="$(cat <<'PYCODE'
print("hello from PyTegBot")
print(sum(i * i for i in range(6)))
PYCODE
)"
fi

json_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
field = sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8"))
value = data
for part in field.split("."):
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("")
else:
    print(value)
PY
}

printf 'Checking health at %s/health\n' "${BASE_URL}"
HEALTH_STATUS="$(curl -sS -o "${HEALTH_BODY}" -w "%{http_code}" "${BASE_URL}/health")"
if [ "${HEALTH_STATUS}" != "200" ]; then
  printf 'Health check failed with HTTP %s\n' "${HEALTH_STATUS}" >&2
  cat "${HEALTH_BODY}" >&2
  exit 1
fi
cat "${HEALTH_BODY}"
printf '\n'

CREATE_STATUS="$(CODE_PAYLOAD="${CODE}" python3 - "${BASE_URL}" "${AUTH_TOKEN}" "${TASK_TIMEOUT}" "${CREATE_BODY}" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

base_url, token, timeout_seconds, output_path = sys.argv[1:5]
payload = {
    "code": os.environ["CODE_PAYLOAD"],
    "source": "api",
    "timeout_seconds": int(timeout_seconds),
}
result = subprocess.run(
    [
        "curl",
        "-sS",
        "-o",
        output_path,
        "-w",
        "%{http_code}",
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Content-Type: application/json",
        "-X",
        "POST",
        f"{base_url}/v1/tasks",
        "-d",
        json.dumps(payload),
    ],
    check=True,
    capture_output=True,
    text=True,
)
sys.stdout.write(result.stdout)
PY
)"

if [ "${CREATE_STATUS}" != "202" ]; then
  printf 'Task creation failed with HTTP %s\n' "${CREATE_STATUS}" >&2
  cat "${CREATE_BODY}" >&2
  exit 1
fi

TASK_ID="$(json_field "${CREATE_BODY}" "task_id")"
printf 'Created task %s\n' "${TASK_ID}"

poll_count=0
while [ "${poll_count}" -lt "${MAX_POLLS}" ]; do
  poll_count=$((poll_count + 1))
  TASK_STATUS_HTTP="$(curl -sS -o "${TASK_BODY}" -w "%{http_code}" -H "Authorization: Bearer ${AUTH_TOKEN}" "${BASE_URL}/v1/tasks/${TASK_ID}")"
  if [ "${TASK_STATUS_HTTP}" != "200" ]; then
    printf 'Task status request failed with HTTP %s\n' "${TASK_STATUS_HTTP}" >&2
    cat "${TASK_BODY}" >&2
    exit 1
  fi

  STATUS="$(json_field "${TASK_BODY}" "status")"
  printf 'Poll %s/%s: %s\n' "${poll_count}" "${MAX_POLLS}" "${STATUS}"

  case "${STATUS}" in
    queued|running)
      sleep "${POLL_INTERVAL}"
      ;;
    succeeded|failed|cancelled|timed_out)
      OUTPUT="$(json_field "${TASK_BODY}" "output")"
      ERROR_TEXT="$(json_field "${TASK_BODY}" "error")"
      EXIT_CODE="$(json_field "${TASK_BODY}" "exit_code")"
      printf 'Final status: %s\n' "${STATUS}"
      printf 'Exit code: %s\n' "${EXIT_CODE:-}"
      if [ -n "${OUTPUT}" ]; then
        printf '%s\n%s\n' 'Output:' "${OUTPUT}"
      fi
      if [ -n "${ERROR_TEXT}" ]; then
        printf '%s\n%s\n' 'Error:' "${ERROR_TEXT}"
      fi
      [ "${STATUS}" = "succeeded" ] || exit 1
      exit 0
      ;;
    *)
      printf 'Unexpected task status: %s\n' "${STATUS}" >&2
      cat "${TASK_BODY}" >&2
      exit 1
      ;;
  esac
done

printf 'Task did not finish within %s polls\n' "${MAX_POLLS}" >&2
cat "${TASK_BODY}" >&2
exit 1
