"""Пул потоков вместо gevent.pool.Pool."""
from concurrent.futures import ThreadPoolExecutor


class Pool:
    def __init__(self, size=None):
        self._executor = ThreadPoolExecutor(max_workers=size or 8)

    def spawn(self, func, *args, **kwargs):
        return self._executor.submit(func, *args, **kwargs)

    def join(self):
        self._executor.shutdown(wait=True)

    def kill(self, *args, **kwargs):
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __len__(self):
        return 0
