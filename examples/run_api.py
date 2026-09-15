"""Start the ChalyCrypto API on an automatically-chosen free port.

Picks a free TCP port via socket(PORT=0), writes it to ./.api_port, then runs uvicorn
on it. The second PowerShell can pick the port up with `Get-Content .api_port`.

  python examples/run_api.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252 — reconfigure stdout for the arrow char.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    port = int(os.environ.get("CHALYBCRYPTO_API_PORT", "0")) or _pick_free_port()

    port_file = Path(".api_port")
    port_file.write_text(str(port), encoding="utf-8")

    bar = "=" * 64
    print()
    print(bar)
    print(f"  ChalyCrypto API  →  http://127.0.0.1:{port}")
    print(f"  Swagger UI       http://127.0.0.1:{port}/docs")
    print(f"  Port saved to    {port_file.resolve()}")
    print(bar, flush=True)
    print()

    cmd = [
        sys.executable, "-m", "uvicorn",
        "chalybcrypto_api.main:app",
        "--host", "127.0.0.1",
        "--port", str(port),
    ]
    try:
        return subprocess.call(cmd)
    finally:
        # Don't leave the stale port behind after the server exits.
        try:
            port_file.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
