"""Per-account poller lockfile for the WeChat addon.

iLink's getUpdates is a single-consumer long-poll: when two processes hold
the same bot_token and both call getUpdates, each call may receive a
different subset of messages, and there is no way for either consumer to
know it is racing. The practical symptom is "inbound messages appear flaky"
— see GH issue #83. This module prevents that by taking an exclusive
fcntl.flock on a per-account lockfile in the user's runtime directory.

The lock key hashes the bot_token (which is the only stable identifier of
the iLink account from the addon's perspective). The lockfile path is
deterministic across processes/working-dirs on the same machine, so a
second poller for the same account on the same host is reliably refused.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


class PollerLockBusy(RuntimeError):
    """Raised when another lingtai-wechat poller already holds this account."""


def _lock_dir() -> Path:
    """Where lockfiles live. ~/.lingtai-wechat/locks/ on POSIX."""
    base = Path.home() / ".lingtai-wechat" / "locks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _account_key(bot_token: str) -> str:
    return hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:16]


def lock_path(bot_token: str) -> Path:
    return _lock_dir() / f"poller-{_account_key(bot_token)}.lock"


class AccountLock:
    """fcntl-based exclusive lock per iLink account.

    Held for the lifetime of the poller. Releases automatically when the
    process exits (kernel drops the flock), so a hard kill leaves no stale
    state requiring cleanup.
    """

    def __init__(self, bot_token: str) -> None:
        self._path = lock_path(bot_token)
        self._fh: IO[str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        """Take the exclusive lock. Raises PollerLockBusy if held elsewhere."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover — non-POSIX (Windows)
            log.warning("fcntl unavailable; poller lockfile disabled on this OS")
            return

        fh = open(self._path, "w", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            existing_pid = _read_existing_pid(self._path)
            raise PollerLockBusy(
                f"Another lingtai-wechat poller is already running for this "
                f"iLink account (lockfile: {self._path}, "
                f"holder PID: {existing_pid or 'unknown'}). "
                f"Stop the other poller before starting this one."
            ) from exc

        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        log.info("Acquired WeChat poller lock for account at %s", self._path)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        # Leave the lockfile on disk (its presence + flock state is what
        # matters); removing it would race with a concurrent acquire().


def _read_existing_pid(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
