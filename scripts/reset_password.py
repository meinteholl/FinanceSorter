"""Reset a Finance Sorter account password.

Stored passwords are one-way hashes — the original text is not recoverable, so
a forgotten password can only be replaced, never read back.

Run this in a NORMAL terminal (not through a sandboxed tool), because the
installed app's database lives under %APPDATA% and sandboxed processes may see
a virtualised copy instead of the real file:

    .venv\\Scripts\\python.exe scripts\\reset_password.py

The new password is typed at a hidden prompt, so it never lands in your shell
history. Pass --dev to target the development database in ./data instead of the
installed app's.
"""
import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    sys.exit("werkzeug is missing — run this with .venv\\Scripts\\python.exe")


def installed_db():
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "FinanceSorter" / "finance.db"


def dev_db():
    return Path(__file__).resolve().parent.parent / "data" / "finance.db"


def main():
    ap = argparse.ArgumentParser(description="Reset a Finance Sorter password.")
    ap.add_argument("--dev", action="store_true",
                    help="target ./data/finance.db instead of the installed app's database")
    ap.add_argument("--email", help="account to reset; omit to be prompted")
    args = ap.parse_args()

    path = dev_db() if args.dev else installed_db()
    print(f"Database: {path}")
    if not path.exists():
        sys.exit("No database at that path. Has the app been run on this machine?")

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    users = conn.execute("SELECT id, name, email FROM users ORDER BY id").fetchall()
    if not users:
        sys.exit("No accounts in this database.")

    print("\nAccounts:")
    for u in users:
        print(f"  [{u['id']}] {u['email']}  ({u['name']})")

    email = args.email or input("\nEmail to reset: ").strip()
    row = next((u for u in users if u["email"].lower() == email.lower()), None)
    if row is None:
        sys.exit(f"No account with email {email!r}.")

    pw1 = getpass.getpass(f"New password for {row['email']}: ")
    if len(pw1) < 8:
        sys.exit("Password must be at least 8 characters.")
    if pw1 != getpass.getpass("Repeat new password: "):
        sys.exit("Passwords did not match — nothing changed.")

    # pbkdf2 rather than werkzeug's current scrypt default: it is verified by
    # the same check_password_hash either way, and pbkdf2 is the format this
    # app's frozen builds are known to handle.
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(pw1, method="pbkdf2:sha256"), row["id"]),
    )
    conn.commit()
    conn.close()
    print(f"\nPassword updated for {row['email']} at "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M')}. Sign in with the new one.")


if __name__ == "__main__":
    main()
