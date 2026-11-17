SELECT
    trip_id,
    started_at_local_date,
    started_at_local_date AS trip_date,
    COUNT(*)::INTEGER AS stage_count,
    SUM(point_count)::BIGINT AS total_points,
    SUM(total_distance_m)::DOUBLE AS total_distance_m,
    SUM(elevation_gain_m)::DOUBLE AS total_elevation_gain_m,
    SUM(elevation_loss_m)::DOUBLE AS total_elevation_loss_m,
    SUM(moving_seconds)::DOUBLE AS total_moving_seconds,
    SUM(elapsed_seconds)::DOUBLE AS total_elapsed_seconds,
    MAX(max_speed_mps)::DOUBLE AS peak_speed_mps,
    AVG(avg_hr_bpm)::DOUBLE AS avg_hr_bpm,
    AVG(avg_watts)::DOUBLE AS avg_watts,
    AVG(avg_cadence)::DOUBLE AS avg_cadence
FROM {{ ref('mart_stage_summary') }}
GROUP BY trip_id, started_at_local_date
