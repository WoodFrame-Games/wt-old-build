"""Bootstrap для steamctl.

Подменяем sys.stdin на прокси: в Windows getpass() иначе пытается читать
пароль через msvcrt из реальной консоли и не работает с перенаправленным
потоком. Прокси заставляет getpass использовать обычный readline(), поэтому
пароль и коды Guard можно вводить в поле «Ввод» приложения.
"""
import getpass
import runpy
import sys
import warnings

warnings.filterwarnings("ignore", category=getpass.GetPassWarning)


class _StdinProxy:
    def __init__(self, stream):
        self._stream = stream

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def readline(self, *args, **kwargs):
        return self._stream.readline(*args, **kwargs)

    def read(self, *args, **kwargs):
        return self._stream.read(*args, **kwargs)

    def __iter__(self):
        return iter(self._stream)


sys.stdin = _StdinProxy(sys.stdin)
sys.argv = ["steamctl"] + sys.argv[1:]
runpy.run_module("steamctl", run_name="__main__")
