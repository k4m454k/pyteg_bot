from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

OUTPUT_DIR_ENV_VAR = "PYTEGBOT_OUTPUT_DIR"
CODE_STDIN_ENV_VAR = "PYTEGBOT_CODE_STDIN"
CODE_FILE_ENV_VAR = "PYTEGBOT_CODE_FILE"
DEFAULT_OUTPUT_DIR = "/tmp/pytegbot-out"
CODE_FILE_WAIT_TIMEOUT_SECONDS = 60.0
CODE_FILE_WAIT_POLL_INTERVAL_SECONDS = 0.05
MATPLOTLIB_CONFIG_DIR = "/tmp/pytegbot-matplotlib"
MATPLOTLIB_CONFIG_SEED_DIR = "/opt/matplotlib-seed"
MANIFEST_FILENAME = ".pytegbot-artifacts.json"
ARTIFACT_READY_MARKER = "__PYTEGBOT_ARTIFACTS_READY__"
ARTIFACT_ACK_FILENAME = ".pytegbot-artifacts.ack"
ARTIFACT_ACK_TIMEOUT_SECONDS = 5.0
ARTIFACT_ACK_POLL_INTERVAL_SECONDS = 0.1
SUPPORTED_IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_DISCOVERED_ARTIFACTS = 256


def export_matplotlib_figures(output_dir: Path) -> None:
    if any(
        path.is_file() and not path.is_symlink() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
        for path in output_dir.rglob("*")
    ):
        return

    pyplot = sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return

    get_fignums = getattr(pyplot, "get_fignums", None)
    if not callable(get_fignums):
        return

    figure_numbers = list(get_fignums())
    if not figure_numbers:
        return

    try:
        for index, figure_number in enumerate(figure_numbers, start=1):
            figure = pyplot.figure(figure_number)
            figure.savefig(output_dir / f"figure-{index}.png")
        pyplot.close("all")
    except Exception:  # noqa: BLE001
        return


def write_artifact_manifest(output_dir: Path) -> int:
    artifacts: list[dict[str, object]] = []
    for path in sorted(output_dir.rglob("*")):
        if len(artifacts) >= MAX_DISCOVERED_ARTIFACTS:
            break
        if not path.is_file() or path.is_symlink():
            continue
        if path.name == MANIFEST_FILENAME:
            continue

        media_type = SUPPORTED_IMAGE_SUFFIXES.get(path.suffix.lower())
        if media_type is None:
            continue

        artifacts.append(
            {
                "relative_path": path.relative_to(output_dir).as_posix(),
                "filename": path.name,
                "media_type": media_type,
                "size_bytes": path.stat().st_size,
            }
        )

    manifest_path = output_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps({"artifacts": artifacts}, ensure_ascii=True),
        encoding="utf-8",
    )
    return len(artifacts)


def wait_for_artifact_pickup(output_dir: Path) -> None:
    ack_path = output_dir / ARTIFACT_ACK_FILENAME
    print(ARTIFACT_READY_MARKER, file=sys.stderr, flush=True)

    deadline = time.monotonic() + ARTIFACT_ACK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ack_path.exists():
            return
        time.sleep(ARTIFACT_ACK_POLL_INTERVAL_SECONDS)


def prepare_runtime_environment() -> None:
    runtime_mpl_dir = Path(MATPLOTLIB_CONFIG_DIR)
    runtime_mpl_dir.mkdir(parents=True, exist_ok=True)

    seed_dir = Path(MATPLOTLIB_CONFIG_SEED_DIR)
    if seed_dir.exists():
        for source in seed_dir.iterdir():
            target = runtime_mpl_dir / source.name
            if target.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)

    os.environ["MPLCONFIGDIR"] = str(runtime_mpl_dir)


def load_code() -> tuple[str | None, int]:
    if os.environ.get(CODE_STDIN_ENV_VAR, "").strip():
        try:
            payload = sys.stdin.buffer.read()
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to read code from stdin: {exc}", file=sys.stderr)
            return None, 2
        if not payload:
            print("Missing code on stdin.", file=sys.stderr)
            return None, 2
        try:
            return payload.decode("utf-8"), 0
        except UnicodeDecodeError as exc:
            print(f"Failed to decode stdin payload: {exc}", file=sys.stderr)
            return None, 2

    code_file = os.environ.get(CODE_FILE_ENV_VAR, "").strip()
    if code_file:
        code_path = Path(code_file)
        deadline = time.monotonic() + CODE_FILE_WAIT_TIMEOUT_SECONDS
        try:
            while True:
                try:
                    return code_path.read_text(encoding="utf-8"), 0
                except FileNotFoundError:
                    if time.monotonic() >= deadline:
                        print(f"Missing code file: {code_file}", file=sys.stderr)
                        return None, 2
                    time.sleep(CODE_FILE_WAIT_POLL_INTERVAL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to read code file: {exc}", file=sys.stderr)
            return None, 2

    encoded = os.environ.get("PYTEGBOT_CODE_B64", "")
    if not encoded:
        print("Missing PYTEGBOT_CODE_B64 environment variable.", file=sys.stderr)
        return None, 2

    try:
        return base64.b64decode(encoded).decode("utf-8"), 0
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to decode payload: {exc}", file=sys.stderr)
        return None, 2


def main() -> int:
    prepare_runtime_environment()
    output_dir = Path(os.environ.get(OUTPUT_DIR_ENV_VAR, DEFAULT_OUTPUT_DIR))
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(output_dir)

    code, load_exit_code = load_code()
    if code is None:
        return load_exit_code

    globals_dict = {"__name__": "__main__"}
    exit_code = 0
    try:
        exec(compile(code, "<pytegbot>", "exec"), globals_dict, globals_dict)
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        exit_code = 1
    finally:
        export_matplotlib_figures(output_dir)
        artifact_count = write_artifact_manifest(output_dir)
        if artifact_count > 0:
            wait_for_artifact_pickup(output_dir)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
