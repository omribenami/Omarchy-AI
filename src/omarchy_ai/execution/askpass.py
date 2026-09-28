"""SSH_ASKPASS for commands the harness runs (see execution/passwords.py).

ssh passes its prompt as the only argument and reads the answer from stdout.
A new host key is accepted (what the user would type), a password prompt gets
the password from GNOME Keyring, and anything else (a key passphrase, an
unknown question) is declined so ssh fails instead of hanging.
"""

from __future__ import annotations

import sys


def answer(prompt: str) -> str | None:
    text = prompt.lower()
    if "(yes/no" in text:
        return "yes"
    if "passphrase" in text:
        return None
    if "password" in text:
        from .passwords import ssh_password
        return ssh_password()
    return None


def main() -> int:
    reply = answer(" ".join(sys.argv[1:]))
    if reply is None:
        return 1
    sys.stdout.write(reply + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
