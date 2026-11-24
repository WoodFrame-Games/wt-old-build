"""Лёгкая совместимая прослойка вместо gevent.

Нужна только для того, чтобы bundled steamctl работал на Python 3.14, для
которого у gevent нет готовых wheel. Реальные потоки Python вместо greenlet.
Для команд `depot info` / `depot download` этого достаточно.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class Timeout(Exception):
    def __init__(self, seconds=None, exception=None, *args, **kwargs):
        super().__init__(*(args or ("timeout",)))
        self.seconds = seconds
        self.exception = exception

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class Greenlet:
    """Минимальный аналог greenlet поверх threading.Thread."""

    def __init__(self, func, args, kwargs):
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self._value = None
        self._exc = None

        def runner():
            try:
                self._value = func(*args, **kwargs)
            except BaseException as exc:  # pragma: no cover
                self._exc = exc

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()

    def get(self, timeout=None):
        self._thread.join(timeout)
        if self._exc is not None:
            raise self._exc
        return self._value

    def join(self, timeout=None):
        self._thread.join(timeout)

    def kill(self, block=True, timeout=None):
        # потоки-демоны завершатся вместе с процессом, принудительно
        # остановить их нельзя — для этого сценария это нормально
        return

    def ready(self):
        return not self._thread.is_alive()

    def successful(self):
        return self.ready() and self._exc is None


def spawn(func, *args, **kwargs):
    return Greenlet(func, args, kwargs)


def sleep(seconds=0, *args, **kwargs):
    time.sleep(max(seconds, 0))


def idle():
    time.sleep(0)


def get_hub():  # pragma: no cover
    return None


from . import event, monkey, pool, queue, select, socket  # noqa: E402,F401

__all__ = [
    "spawn", "sleep", "idle", "Timeout", "Greenlet", "get_hub",
    "event", "monkey", "pool", "queue", "select", "socket",
]
