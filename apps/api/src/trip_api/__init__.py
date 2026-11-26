"""Trip Animator hardened FastAPI backend."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trip_api.main import app, create_app

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        # Tooling such as S3 bootstrap must not initialize the web app on import.
        from trip_api import main

        return getattr(main, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
