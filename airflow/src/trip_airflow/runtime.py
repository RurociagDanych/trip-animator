"""Runtime lifecycle helpers for pure-Python Airflow task modules."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from trip_api.config import Settings
from trip_api.db import create_db_engine, create_session_factory
from trip_api.reaper import build_object_store
from trip_api.storage import ObjectStore


@dataclass(frozen=True, slots=True)
class AirflowRuntime:
    """Initialized application settings, DB session factory, and ObjectStore."""

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    store: ObjectStore


@contextmanager
def open_runtime(settings: Settings | None = None) -> Iterator[AirflowRuntime]:
    """Initialize DB engine, session factory, and ObjectStore, disposing engine on exit."""
    cfg = settings or Settings()
    engine = create_db_engine(cfg.database_url)
    session_factory = create_session_factory(engine)
    store = build_object_store(cfg)
    try:
        yield AirflowRuntime(
            settings=cfg,
            engine=engine,
            session_factory=session_factory,
            store=store,
        )
    finally:
        engine.dispose()


def resolve_gold_output_dir(gold_dir: Path | str | None = None) -> Path:
    """Resolve the Gold DuckDB output directory from argument or GOLD_OUTPUT_DIR env."""
    if gold_dir is not None:
        return Path(gold_dir).resolve()
    env_val = os.environ.get("GOLD_OUTPUT_DIR", "").strip()
    if env_val:
        return Path(env_val).resolve()
    return Path("./data/gold").resolve()
