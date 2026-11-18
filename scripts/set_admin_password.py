#!/usr/bin/env python3
"""Set the password for the admin dashboard (admin.smartgarden.tech, port 8502).

    .venv/bin/python scripts/set_admin_password.py              # type the password (not echoed)
    .venv/bin/python scripts/set_admin_password.py --generate   # make a random one and print it

Writes the password's scrypt hash and a fresh cookie-signing secret to the top
of .streamlit/secrets.toml (git-ignored); the password itself is not stored.
A new password logs out every browser. No restart needed.
"""

from __future__ import annotations

import getpass
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from admin_auth import SECRETS_PATH, hash_password  # noqa: E402

MIN_LENGTH = 12


def write_secrets(password: str, path: Path = SECRETS_PATH) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines
             if not re.match(r"\s*admin_(password_hash|cookie_secret)\s*=", line)]
    # Top-level keys go above any [table] already in the file.
    head = [f'admin_password_hash = "{hash_password(password)}"',
            f'admin_cookie_secret = "{secrets.token_hex(32)}"']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(head + lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    if "--generate" in sys.argv[1:]:
        password = secrets.token_urlsafe(18)
        print(f"Admin password: {password}\nSave it now; it is not stored anywhere.")
    else:
        password = getpass.getpass("New admin password: ")
        if len(password) < MIN_LENGTH:
            print(f"Use at least {MIN_LENGTH} characters.", file=sys.stderr)
            return 1
        if getpass.getpass("Again: ") != password:
            print("The two passwords differ.", file=sys.stderr)
            return 1
    write_secrets(password)
    print(f"Saved to {SECRETS_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
