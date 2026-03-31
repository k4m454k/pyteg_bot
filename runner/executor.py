from __future__ import annotations

import base64
import os
import sys
import traceback


def main() -> int:
    encoded = os.environ.get("PYTEGBOT_CODE_B64", "")
    if not encoded:
        print("Missing PYTEGBOT_CODE_B64 environment variable.", file=sys.stderr)
        return 2

    try:
        code = base64.b64decode(encoded).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to decode payload: {exc}", file=sys.stderr)
        return 2

    globals_dict = {"__name__": "__main__"}
    try:
        exec(compile(code, "<pytegbot>", "exec"), globals_dict, globals_dict)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
