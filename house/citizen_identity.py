"""Private continuity credentials for End of Line citizens.

An identity key is not a persona and never enters a model prompt. It is the
opaque proof the arena issued so a returning process receives the same public
designation. Seat tokens remain separate, short-lived room credentials.
"""

import os
import re
import stat
import tempfile


IDENTITY_KEY = re.compile(r"^[0-9a-f]{48}$")
IDENTITY_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class IdentityError(ValueError):
    """Continuity state is absent from, malformed in, or conflicts with a join."""


def _path(dirpath, label):
    if not isinstance(label, str) or not IDENTITY_LABEL.fullmatch(label):
        raise IdentityError("identity label must contain only letters, digits, dot, dash, or underscore")
    return os.path.join(os.path.abspath(os.path.expanduser(dirpath)), "identities", label + ".key")


def _valid(key):
    return isinstance(key, str) and IDENTITY_KEY.fullmatch(key) is not None


def load_identity(dirpath, label):
    """Return this citizen's retained identity key, or None before first join.

    A malformed or non-private file is never treated as "no identity": silently
    minting a replacement would be an identity reset disguised as recovery.
    """
    path = _path(dirpath, label)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise IdentityError(f"cannot open retained identity: {e}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise IdentityError("retained identity is not a regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r", encoding="ascii") as f:
            fd = -1
            key = f.read(128).strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if not _valid(key):
        raise IdentityError("retained identity is malformed; refusing to replace it")
    return key


def join_body(meta, identity_key):
    """Build the strict join body without ever putting identity into metadata."""
    body = {"meta": meta}
    if identity_key is not None:
        if not _valid(identity_key):
            raise IdentityError("identity key is malformed")
        body["identity_key"] = identity_key
    return body


def retain_identity(dirpath, label, current, response):
    """Validate and durably retain the identity established by a successful join.

    The arena returns identity_key only when it minted one. A returning join must
    not echo or rotate it. Existing identity is never overwritten implicitly.
    """
    if not isinstance(response, dict):
        raise IdentityError("join response is not an object")
    issued = response.get("identity_key")
    if current is not None:
        if not _valid(current):
            raise IdentityError("current identity key is malformed")
        if issued is not None and issued != current:
            raise IdentityError("arena tried to replace an existing identity")
        return current
    if not _valid(issued):
        raise IdentityError("first join did not return a valid identity key")

    path = _path(dirpath, label)
    base = os.path.dirname(path)
    os.makedirs(base, mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)

    existing = load_identity(dirpath, label)
    if existing is not None:
        if existing != issued:
            raise IdentityError("another process established a different identity for this citizen")
        return existing

    fd, tmp = tempfile.mkstemp(prefix=".identity-", dir=base, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            fd = -1
            f.write(issued + "\n")
            f.flush()
            os.fsync(f.fileno())
        # Link, rather than replace: creation of the final path is atomic and
        # refuses to overwrite a winner. Two copies of one citizen starting
        # together may both have joined, but neither may silently win a race that
        # changes which life the slot resumes next time.
        try:
            os.link(tmp, path)
        except FileExistsError:
            existing = load_identity(dirpath, label)
            if existing != issued:
                raise IdentityError("identity file appeared with a different key")
            return existing
        return issued
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
