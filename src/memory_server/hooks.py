"""Named fault-injection points; no-ops unless a test registers a callback."""

_hooks = {}


def register(name, fn):
    _hooks[name] = fn


def clear():
    _hooks.clear()


def fire(name):
    fn = _hooks.get(name)
    if fn is not None:
        fn()
