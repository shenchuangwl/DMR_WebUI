"""Crash-safe, lockable JSON state shared by strategy processes.

Two defects motivated this module:

1. ``open(path, 'w')`` truncates before writing. A crash mid-``json.dump`` leaves
   a half-written file; the readers then swallowed ``JSONDecodeError`` and
   silently reported "no position", losing the live stop-loss order id.
2. Strategy objects are rebuilt every scheduling cycle, so any counter kept only
   on ``self`` is permanently zero.

Writes here are atomic (temp file in the same directory, ``fsync``, then
``os.replace``) and a corrupt file is never silently treated as empty: it is
quarantined and reported, so the caller can fail safe instead of trading on a
blank slate.
"""
import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = PROJECT_ROOT / 'data' / 'runtime_state'


class StateCorruptError(RuntimeError):
    """Raised when a state file exists but cannot be parsed."""


def state_path(name):
    """Return a private path under the project's runtime-state directory.

    The directory is created 0700. Runtime state must never live in a
    world-writable shared location such as /tmp, where any local user can read
    open positions or forge stop-loss order ids.
    """
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_ROOT, 0o700)
    except OSError:
        pass
    safe = str(name).replace('/', '_').replace(os.sep, '_')
    return STATE_ROOT / safe


def atomic_write_json(path, payload):
    """Write ``payload`` so readers observe either the old or the new file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, default=str, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def read_json(path, default=None):
    """Return parsed JSON, ``default`` if absent, and raise if corrupt.

    A corrupt file is renamed to ``<name>.corrupt`` so the next run starts clean
    while the damaged content stays available for investigation.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise StateCorruptError('cannot read %s: %s' % (path, exc)) from exc
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        quarantine = path.with_suffix(path.suffix + '.corrupt')
        try:
            os.replace(str(path), str(quarantine))
        except OSError:
            pass
        raise StateCorruptError(
            'state file %s was corrupt and has been moved to %s: %s' % (path, quarantine, exc)
        ) from exc


@contextmanager
def file_lock(name, blocking=True):
    """Cross-process advisory lock, used to serialise read-modify-write cycles."""
    lock_file = state_path(str(name) + '.lock')
    handle = open(str(lock_file), 'a+')
    try:
        os.chmod(str(lock_file), 0o600)
    except OSError:
        pass
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise BlockingIOError('state %r is locked by another process' % name) from exc
        raise
    try:
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def single_instance(name):
    """Refuse to start when another process already holds this entrypoint lock.

    Two copies of the same strategy means every signal is acted on twice: double
    size on entry and stop-loss orders that cancel each other's ids.
    """
    lock_file = state_path('instance_' + str(name) + '.lock')
    handle = open(str(lock_file), 'a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            'another instance of %r is already running (lock: %s). '
            'Refusing to start a second copy, which would double every order.' % (name, lock_file)
        ) from exc
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
