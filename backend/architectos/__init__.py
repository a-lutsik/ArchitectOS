"""ArchitectOS local-first application backend and memory SDK."""

__all__ = ["ArchitectOS", "__version__"]

__version__ = "1.0.0"


def __getattr__(name: str) -> type:
    # The SDK loads on first use: agent hooks import this package on every agent
    # turn and never need it.
    if name == "ArchitectOS":
        from .sdk import ArchitectOS

        return ArchitectOS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
