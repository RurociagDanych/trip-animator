{{
    config(
        materialized='incremental',
        unique_key='stage_id',
        incremental_strategy='delete+insert',
        on_schema_change='append_new_columns',
        post_hook="{% if is_incremental() %} DELETE FROM {{ this }} WHERE stage_id NOT IN (SELECT DISTINCT stage_id FROM {{ ref('stg_telemetry_points') }}); {% endif %}"
    )
}}
WITH base AS (
    SELECT
        trip_id,
        stage_id,
        seq,
        segment_idx,
        ts_utc,
        lat,
        lon,
        elev_m,
        delta_dist_m,
        cum_dist_m,
        speed_mps,
        grade_pct,
        hr_bpm,
        cadence,
        watts,
        is_moving,
        CASE
            WHEN segment_idx = LAG(segment_idx) OVER (PARTITION BY trip_id, stage_id ORDER BY seq)
            THEN elev_m - LAG(elev_m) OVER (PARTITION BY trip_id, stage_id ORDER BY seq)
            ELSE NULL
        END AS delta_elev_m,
        CASE
            WHEN segment_idx = LAG(segment_idx) OVER (PARTITION BY trip_id, stage_id ORDER BY seq)
                AND ts_utc IS NOT NULL
                AND LAG(ts_utc) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) IS NOT NULL
            THEN GREATEST(
                EPOCH(ts_utc) - EPOCH(LAG(ts_utc) OVER (PARTITION BY trip_id, stage_id ORDER BY seq)),
                0.0
            )
            ELSE 0.0
        END AS delta_sec
    FROM {{ ref('stg_telemetry_points') }}
),
agg AS (
    SELECT
        trip_id,
        stage_id,
        COUNT(*)::BIGINT AS point_count,
        MIN(ts_utc) AS started_at_utc,
        MAX(ts_utc) AS ended_at_utc,
        COALESCE(
            CAST(
                DATE_TRUNC(
                    'day',
                    MIN(ts_utc) + CAST(ROUND(AVG(lon) / 15.0) AS INTEGER) * INTERVAL '1 hour'
                ) AS DATE
            ),
            DATE '1970-01-01'
        ) AS started_at_local_date,
        COALESCE(MAX(cum_dist_m) - MIN(cum_dist_m), 0.0)::DOUBLE AS total_distance_m,
        COALESCE(SUM(CASE WHEN delta_elev_m > 0 THEN delta_elev_m ELSE 0.0 END), 0.0)::DOUBLE AS elevation_gain_m,
        COALESCE(SUM(CASE WHEN delta_elev_m < 0 THEN ABS(delta_elev_m) ELSE 0.0 END), 0.0)::DOUBLE AS elevation_loss_m,
        COALESCE(SUM(CASE WHEN is_moving THEN delta_sec ELSE 0.0 END), 0.0)::DOUBLE AS moving_seconds,
        COALESCE(SUM(delta_sec), 0.0)::DOUBLE AS elapsed_seconds,
        COALESCE(AVG(NULLIF(speed_mps, 0.0)), 0.0)::DOUBLE AS avg_moving_speed_mps,
        COALESCE(MAX(speed_mps), 0.0)::DOUBLE AS max_speed_mps,
        MAX(grade_pct)::DOUBLE AS max_grade_pct,
        AVG(hr_bpm)::DOUBLE AS avg_hr_bpm,
        AVG(cadence)::DOUBLE AS avg_cadence,
        AVG(watts)::DOUBLE AS avg_watts,
        MIN(lat)::DOUBLE AS min_lat,
        MAX(lat)::DOUBLE AS max_lat,
        MIN(lon)::DOUBLE AS min_lon,
        MAX(lon)::DOUBLE AS max_lon
    FROM base
    GROUP BY trip_id, stage_id
),
final AS (
    SELECT
        *,
        CASE
            WHEN moving_seconds > 0 THEN (elevation_gain_m / moving_seconds) * 3600.0
            WHEN elapsed_seconds > 0 THEN (elevation_gain_m / elapsed_seconds) * 3600.0
            ELSE 0.0
        END::DOUBLE AS vam_m_per_h
    FROM agg
)
SELECT
    final.*,
    '{{ invocation_id }}' AS _dbt_invocation_id
FROM final
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1
    FROM {{ this }} AS existing
    WHERE existing.stage_id = final.stage_id
      AND existing.point_count = final.point_count
      AND ABS(existing.total_distance_m - final.total_distance_m) < 1e-6
      AND ABS(existing.elevation_gain_m - final.elevation_gain_m) < 1e-6
      AND existing.started_at_utc IS NOT DISTINCT FROM final.started_at_utc
      AND existing.ended_at_utc IS NOT DISTINCT FROM final.ended_at_utc
)
{% endif %}
