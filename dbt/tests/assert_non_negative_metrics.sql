-- Singular data quality test: all physical telemetry KPIs must be non-negative.
SELECT
    stage_id,
    'mart_stage_summary' AS source_table,
    total_distance_m,
    elevation_gain_m,
    elevation_loss_m,
    moving_seconds,
    elapsed_seconds,
    avg_moving_speed_mps,
    max_speed_mps,
    vam_m_per_h
FROM {{ ref('mart_stage_summary') }}
WHERE total_distance_m < 0.0
   OR elevation_gain_m < 0.0
   OR elevation_loss_m < 0.0
   OR moving_seconds < 0.0
   OR elapsed_seconds < 0.0
   OR avg_moving_speed_mps < 0.0
   OR max_speed_mps < 0.0
   OR vam_m_per_h < 0.0
   OR point_count <= 0

UNION ALL

SELECT
    trip_id || ':' || CAST(trip_date AS VARCHAR) AS stage_id,
    'mart_trip_daily_summary' AS source_table,
    total_distance_m,
    total_elevation_gain_m AS elevation_gain_m,
    total_elevation_loss_m AS elevation_loss_m,
    total_moving_seconds AS moving_seconds,
    total_elapsed_seconds AS elapsed_seconds,
    0.0 AS avg_moving_speed_mps,
    peak_speed_mps AS max_speed_mps,
    0.0 AS vam_m_per_h
FROM {{ ref('mart_trip_daily_summary') }}
WHERE total_distance_m < 0.0
   OR total_elevation_gain_m < 0.0
   OR total_elevation_loss_m < 0.0
   OR total_moving_seconds < 0.0
   OR total_elapsed_seconds < 0.0
   OR peak_speed_mps < 0.0
   OR stage_count <= 0
   OR total_points <= 0
