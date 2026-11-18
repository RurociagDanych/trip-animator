"""Airflow adapter re-exporting canonical `trip_api.ingest` worker functions."""

from __future__ import annotations

from trip_api.ingest import (
    StravaUpstreamError,
    _claim_ingest_jobs,
    _delete_stage_and_artifacts,
    _is_rate_limited,
    _rewrite_stage_from_parsed,
    cluster_activity_day_index,
    process_pending_ingest_jobs,
    reap_exhausted_ingest_jobs,
    replay_outdated_silver_stages,
)

__all__ = [
    "StravaUpstreamError",
    "_claim_ingest_jobs",
    "_delete_stage_and_artifacts",
    "_is_rate_limited",
    "_rewrite_stage_from_parsed",
    "cluster_activity_day_index",
    "process_pending_ingest_jobs",
    "reap_exhausted_ingest_jobs",
    "replay_outdated_silver_stages",
]
