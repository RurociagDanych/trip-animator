-- Singular data quality test: segment continuity, non-decreasing timestamps, and non-negative delta distances.
WITH ordered AS (
    SELECT
        trip_id,
        stage_id,
        seq,
        segment_idx,
        ts_utc,
        delta_dist_m,
        speed_mps,
        LAG(seq) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_seq,
        LAG(segment_idx) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_segment_idx,
        LAG(ts_utc) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_ts_utc
    FROM {{ ref('stg_telemetry_points') }}
)
SELECT
    trip_id,
    stage_id,
    seq,
    segment_idx,
    delta_dist_m,
    speed_mps
FROM ordered
WHERE segment_idx < 0
   OR delta_dist_m < 0.0
   OR speed_mps < 0.0
   OR (prev_seq IS NOT NULL AND seq <= prev_seq)
   OR (prev_segment_idx IS NOT NULL AND segment_idx < prev_segment_idx)
   OR (prev_ts_utc IS NOT NULL AND segment_idx = prev_segment_idx AND ts_utc < prev_ts_utc)
