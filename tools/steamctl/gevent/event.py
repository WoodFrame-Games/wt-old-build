"""Совместимые Event / AsyncResult на основе threading."""
import threading

from . import Timeout


class Event:
    def __init__(self):
        self._event = threading.Event()

    def set(self):
        self._event.set()

    def clear(self):
        self._event.clear()

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)


class AsyncResult:
    def __init__(self):
        self._event = threading.Event()
        self._value = None
        self._exception = None

    def set(self, value=None):
        self._value = value
        self._event.set()

    def set_exception(self, exception):
        self._exception = exception
        self._event.set()

    def get(self, block=True, timeout=None):
        if block:
            wait_timeout = timeout
        else:
            wait_timeout = 0
        if not self._event.wait(wait_timeout):
            raise Timeout()
        if self._exception is not None:
            raise self._exception
        return self._value

    def get_nowait(self):
        return self.get(block=False)

    def wait(self, timeout=None):
        return self._event.wait(timeout)

    def ready(self):
        return self._event.is_set()

    def successful(self):
        return self.ready() and self._exception is None

    def clear(self):
        self._event.clear()
        self._value = None
        self._exception = None
