"""Daybagger real-time IPC signal listener module."""


def start_listener():
    """Import lazily so ``python -m`` does not pre-load the target module."""
    from daybagger.engine.signal_listener import start_listener as run_listener

    return run_listener()


__all__ = ["start_listener"]
