"""Local Jev credential storage, separate from plugin installs and checkouts."""

import os
from pathlib import Path
import stat
import tempfile


def key_path():
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "orca-agent-router" / "jev-key"


def validate_key(value):
    value = value.strip()
    if not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Enter a nonempty Jev key without whitespace (maximum 4,096 characters)")
    return value


def read_jev_key():
    # Explicit shell configuration takes precedence over a saved connection.
    if os.environ.get("TYPESAFE_API_KEY"):
        return validate_key(os.environ["TYPESAFE_API_KEY"])
    try:
        fd = os.open(key_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("Cannot read saved Jev key; reconnect Jev") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Saved Jev key must be an owner-only regular file (chmod 600)")
        return validate_key(source.read(4097))


def save_jev_key(value):
    value = validate_key(value)
    path = key_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Credential directory must be owned by you with mode 700")
    fd, temporary = tempfile.mkstemp(prefix=".jev-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(value + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remove_jev_key():
    key_path().unlink(missing_ok=True)
