"""Opt-in absolute native-test budget; unbound production/default calls unchanged."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import os
import signal
import sys
import time

STATE = ContextVar("synthetic_native_absolute_deadline", default=None)


def clamp(seconds):
    state = STATE.get()
    if state is None:
        return seconds
    if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds <= 0:
        raise TimeoutError("invalid native phase budget")
    remaining = state[0] - time.monotonic()
    if not 0 < remaining <= 600:
        raise TimeoutError("original shared native deadline expired")
    return min(seconds, remaining)


@contextmanager
def scope(active_deadline, total_deadline):
    if not all(type(value) in {int, float} and math.isfinite(value) for value in (active_deadline, total_deadline)):
        raise TimeoutError("invalid absolute native deadline")
    if not 0 < active_deadline <= total_deadline or not 0 < total_deadline - time.monotonic() <= 600:
        raise TimeoutError("original native budget expired or replaced")
    inherited = STATE.get()
    if inherited is not None and (active_deadline > inherited[0] or total_deadline > inherited[1]):
        raise TimeoutError("nested native scope cannot expand the inherited budget")
    token = STATE.set((active_deadline, total_deadline))
    try:
        yield
    finally:
        STATE.reset(token)


@contextmanager
def final_phase():
    state = STATE.get()
    if state is None:
        yield
        return
    token = STATE.set((state[1], state[1]))
    try:
        yield
    finally:
        STATE.reset(token)


@contextmanager
def process(factory, *args, **kwargs):
    if STATE.get() is None:
        with factory(*args, **kwargs) as child:
            yield child
        return
    clamp(600)
    child = factory(*args, **kwargs)
    try:
        yield child
    finally:
        # Popen.__exit__ waits without a timeout. Opt-in clients instead use
        # finish_client and close only their own pipes; never a daemon's FDs.
        primary, failure = sys.exc_info()[1], None
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as error:
                    if primary:
                        primary.add_note("native client pipe close failed: " + type(error).__name__)
                    elif failure is None:
                        failure = error
        if failure:
            raise failure


def finish_client(child, primary=None):
    try:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)  # Original client group, never dockerd.
        if STATE.get() is None:
            child.wait()
        else:
            child.wait(timeout=clamp(3))
    except BaseException as error:
        if primary:
            primary.add_note("native client exit unconfirmed: " + type(error).__name__)
        else:
            raise
