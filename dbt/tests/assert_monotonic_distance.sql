-- Singular data quality test: cumulative distance must never decrease within a stage.
WITH ordered AS (
    SELECT
        trip_id,
        stage_id,
        seq,
        cum_dist_m,
        LAG(cum_dist_m) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_cum_dist_m
    FROM {{ ref('stg_telemetry_points') }}
)
SELECT
    trip_id,
    stage_id,
    seq,
    prev_cum_dist_m,
    cum_dist_m
FROM ordered
WHERE prev_cum_dist_m IS NOT NULL
  AND cum_dist_m < prev_cum_dist_m
