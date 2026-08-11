"""Reset a Finance Sorter account password.

Stored passwords are one-way hashes — the original text is not recoverable, so
a forgotten password can only be replaced, never read back.

This is a single file with NO dependencies: it only uses the Python standard
library, so it runs on a machine that has just the installed app and a plain
Python, with nothing to pip install. Copy it across and run:

    python reset_password.py

By default it edits the installed app's database under %APPDATA%. Use --dev for
the development database in ./data, or --db to point at an explicit file.

Close Finance Sorter before running — this writes to the database directly.

The new password is typed at a hidden prompt, so it never lands in your shell
history or in the process list.
"""
import argparse
import getpass
import hashlib
import os
import secrets
import sqlite3
import string
import sys
from datetime import datetime
from pathlib import Path

# Matches werkzeug.security.generate_password_hash(method="pbkdf2:sha256"),
# which is what the app uses to verify. The stored string is
# "pbkdf2:sha256:<iterations>$<salt>$<hex digest>" with the salt taken as UTF-8
# bytes. Reproducing that here is what lets this script drop the dependency.
SALT_CHARS = string.ascii_letters + string.digits
SALT_LENGTH = 16
ITERATIONS = 1_000_000


def hash_password(password: str) -> str:
    salt = "".join(secrets.choice(SALT_CHARS) for _ in range(SALT_LENGTH))
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), ITERATIONS
    ).hex()
    return f"pbkdf2:sha256:{ITERATIONS}${salt}${digest}"


def installed_db() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "FinanceSorter" / "finance.db"


def dev_db() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "finance.db"


def main():
    ap = argparse.ArgumentParser(description="Reset a Finance Sorter password.")
    ap.add_argument("--dev", action="store_true",
                    help="target ./data/finance.db instead of the installed app's database")
    ap.add_argument("--db", help="path to a specific finance.db")
    ap.add_argument("--email", help="account to reset; omit to be prompted")
    args = ap.parse_args()

    if args.db:
        path = Path(args.db)
    else:
        path = dev_db() if args.dev else installed_db()

    print(f"Database: {path}")
    if not path.exists():
        sys.exit("No database at that path. Has the app been run on this machine?")

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        users = conn.execute("SELECT id, name, email FROM users ORDER BY id").fetchall()
    except sqlite3.DatabaseError as e:
        sys.exit(f"Could not read that file as a Finance Sorter database: {e}")
    if not users:
        sys.exit("No accounts in this database.")

    print("\nAccounts:")
    for u in users:
        print(f"  [{u['id']}] {u['email']}  ({u['name']})")

    email = args.email or input("\nEmail to reset: ").strip()
    row = next((u for u in users if u["email"].lower() == email.lower()), None)
    if row is None:
        sys.exit(f"No account with email {email!r}.")

    # Printed text is deliberately plain ASCII: this script is meant to be
    # copied to another machine, where the console codepage is unknown and a
    # stray em-dash can raise UnicodeEncodeError instead of resetting anything.
    print("(nothing appears while you type - that is intentional)")
    pw1 = getpass.getpass(f"New password for {row['email']}: ")
    if len(pw1) < 8:
        sys.exit("Password must be at least 8 characters.")
    if pw1 != getpass.getpass("Repeat new password: "):
        sys.exit("Passwords did not match - nothing changed.")

    print("Hashing...", end="", flush=True)        # a million rounds takes a beat
    new_hash = hash_password(pw1)
    print(" done.")

    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, row["id"])
    )
    conn.commit()
    conn.close()
    print(f"\nPassword updated for {row['email']} at "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M')}. Sign in with the new one.")


if __name__ == "__main__":
    main()
