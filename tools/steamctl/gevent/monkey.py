"""Заглушки gevent.monkey: реальные сокеты уже работают в потоках Python."""

def patch_socket(*args, **kwargs):
    return None


def patch_ssl(*args, **kwargs):
    return None


def patch_select(*args, **kwargs):
    return None


def patch_dns(*args, **kwargs):
    return None


def patch_all(*args, **kwargs):
    return None


def patch_thread(*args, **kwargs):
    return None
