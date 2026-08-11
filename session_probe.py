"""Field test: log whether the current session cookie is still accepted.

Unlike myguichet_get_new_messages.py, this deliberately does NOT refresh an
expired session. Auto-healing would hide the exact moment the original
cookie died and corrupt the measurement.
"""

from __future__ import annotations

from datetime import datetime, timezone

from client import MyGuichetClient, MyGuichetError, SessionExpired
from config import ROOT, get_language, get_space_id, load_environment
from storage import restrict_file

COOKIE_FILE = ROOT / "cookie.txt"
LOG_FILE = ROOT / "session_probe.log"


def log(status: str) -> int:
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"{timestamp} {status}"
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line)
    return 0


def main() -> int:
    load_environment()
    if not COOKIE_FILE.exists():
        return log("NO_COOKIE")
    restrict_file(COOKIE_FILE)
    cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not cookie:
        return log("NO_COOKIE")

    client = MyGuichetClient(cookie, get_space_id(), get_language())
    try:
        client.list_communications(page=1, per_page=1)
    except SessionExpired:
        return log("EXPIRED")
    except MyGuichetError as error:
        return log(f"ERROR {error}")
    else:
        return log("ALIVE")
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
