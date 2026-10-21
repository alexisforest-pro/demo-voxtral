#!/usr/bin/env python3
"""Vibe post_agent hook: run ``make check`` and emit a JSON allow/deny verdict.

Always prints one JSON object and exits 0 so Vibe parses stdout instead of
fail-opening. Disable by removing ``.vibe/hooks.toml``.
"""

from __future__ import annotations

import json
import subprocess
import sys

MAX_OUTPUT = 8192
TIMEOUT = 170


def main() -> None:
    try:
        result = subprocess.run(
            ["make", "--no-print-directory", "check"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=TIMEOUT,
        )
        output = result.stdout
        rc = result.returncode
    except Exception as exc:  # noqa: BLE001 -- hook must always emit JSON
        payload = {
            "decision": "deny",
            "reason": f"make check could not run: {exc}",
        }
    else:
        if rc == 0:
            payload = {"decision": "allow", "system_message": "make check passed"}
        else:
            body = output.strip()[-MAX_OUTPUT:]
            reason = (
                "make check found the following issues; consider fixing them:\n" + body
                if body
                else f"make check failed with no captured output (rc={rc})"
            )
            payload = {"decision": "deny", "reason": reason}

    print(json.dumps(payload), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 -- never let Vibe see a non-zero exit
        print(json.dumps({"decision": "deny", "reason": f"hook crashed: {exc}"}))
    sys.exit(0)
