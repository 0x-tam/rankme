"""Stop the background worker's current job on request.

RankMe runs one job at a time. A stop request terminates the job's Codex child process and
makes the next progress checkpoint raise JobCancelled. Sections that must not be interrupted
(publishing to a live website) run inside `protected()`, where stop requests are refused.
"""
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager

_requested = threading.Event()
_shielded = threading.Event()
_children = set()
_lock = threading.Lock()


class JobCancelled(Exception):
    pass


def reset():
    _requested.clear()


def request():
    """Ask the running job to stop. Returns False while it is inside a protected section."""
    if _shielded.is_set():
        return False
    _requested.set()
    with _lock:
        children = list(_children)
    for child in children:
        _terminate(child)
    return True


def check():
    if _requested.is_set() and not _shielded.is_set():
        raise JobCancelled()


@contextmanager
def protected():
    _shielded.set()
    try:
        yield
    finally:
        _shielded.clear()


def _terminate(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        child.wait(5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def run(command, input=None, stdout=None, stderr=None, text=True, timeout=None, env=None, cwd=None):
    """subprocess.run replacement whose child (and its own children) stop when the job is stopped."""
    check()
    child = subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                             stdout=stdout, stderr=stderr, text=text, env=env, cwd=cwd, start_new_session=True)
    with _lock:
        _children.add(child)
    try:
        if input is not None:
            try:
                child.stdin.write(input)
                child.stdin.close()
            except BrokenPipeError:
                pass
        deadline = time.monotonic() + timeout if timeout else None
        while child.poll() is None:
            if _requested.is_set():
                _terminate(child)
                raise JobCancelled()
            if deadline and time.monotonic() > deadline:
                _terminate(child)
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.2)
        if _requested.is_set():
            raise JobCancelled()
        return subprocess.CompletedProcess(command, child.returncode)
    finally:
        with _lock:
            _children.discard(child)
