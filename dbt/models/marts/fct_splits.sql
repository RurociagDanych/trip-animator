-- depends_on: {{ ref('mart_stage_summary') }}
{{
    config(
        materialized='incremental',
        unique_key='stage_id',
        incremental_strategy='delete+insert',
        pre_hook="{% if is_incremental() %} DELETE FROM {{ this }} WHERE stage_id NOT IN (SELECT stage_id FROM {{ ref('mart_stage_summary') }}) OR stage_id IN (SELECT stage_id FROM {{ ref('mart_stage_summary') }} WHERE _dbt_invocation_id = '{{ invocation_id }}'); {% endif %}"
    )
}}
WITH base AS (
    SELECT
        trip_id,
        stage_id,
        seq,
        segment_idx,
        ts_utc,
        elev_m,
        speed_mps,
        grade_pct,
        hr_bpm,
        watts,
        cum_dist_m - FIRST_VALUE(cum_dist_m) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS rel_dist_m,
        LAG(segment_idx) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_seg,
        LAG(elev_m) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_elev_m,
        LAG(ts_utc) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_ts_utc
    FROM {{ ref('stg_telemetry_points') }}
    {% if is_incremental() %}
    WHERE stage_id IN (
        SELECT stage_id
        FROM {{ ref('mart_stage_summary') }}
        WHERE _dbt_invocation_id = '{{ invocation_id }}'
    )
    {% endif %}
),
bucketed AS (
    SELECT
        trip_id,
        stage_id,
        (FLOOR(GREATEST(rel_dist_m, 0.0) / 1000.0)::INTEGER + 1) AS split_index,
        rel_dist_m,
        speed_mps,
        grade_pct,
        hr_bpm,
        watts,
        CASE
            WHEN prev_seg = segment_idx AND elev_m IS NOT NULL AND prev_elev_m IS NOT NULL
            THEN elev_m - prev_elev_m
            ELSE 0.0
        END AS d_elev,
        CASE
            WHEN prev_seg = segment_idx AND ts_utc IS NOT NULL AND prev_ts_utc IS NOT NULL
            THEN GREATEST(EPOCH(ts_utc) - EPOCH(prev_ts_utc), 0.0)
            ELSE 0.0
        END AS d_sec
    FROM base
)
SELECT
    stage_id || ':split:' || CAST(split_index AS VARCHAR) AS split_id,
    trip_id,
    stage_id,
    split_index,
    ROUND(MIN(rel_dist_m), 1)::DOUBLE AS start_dist_m,
    ROUND(MAX(rel_dist_m), 1)::DOUBLE AS end_dist_m,
    ROUND(GREATEST(MAX(rel_dist_m) - MIN(rel_dist_m), 0.0), 1)::DOUBLE AS distance_m,
    ROUND(SUM(d_sec), 1)::DOUBLE AS duration_s,
    ROUND(SUM(CASE WHEN d_elev > 0 THEN d_elev ELSE 0.0 END), 1)::DOUBLE AS elev_gain_m,
    ROUND(SUM(CASE WHEN d_elev < 0 THEN ABS(d_elev) ELSE 0.0 END), 1)::DOUBLE AS elev_loss_m,
    ROUND(
        CASE
            WHEN SUM(d_sec) > 0 THEN GREATEST(MAX(rel_dist_m) - MIN(rel_dist_m), 0.0) / SUM(d_sec)
            ELSE COALESCE(AVG(speed_mps), 0.0)
        END,
        3
    )::DOUBLE AS avg_speed_mps,
    ROUND(AVG(grade_pct), 2)::DOUBLE AS avg_grade_pct,
    ROUND(AVG(hr_bpm), 1)::DOUBLE AS avg_hr_bpm,
    ROUND(AVG(watts), 1)::DOUBLE AS avg_watts
FROM bucketed
GROUP BY trip_id, stage_id, split_index
