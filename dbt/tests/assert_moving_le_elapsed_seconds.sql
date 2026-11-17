-- Singular test: moving seconds must never exceed total elapsed seconds.
SELECT
    stage_id,
    moving_seconds,
    elapsed_seconds
FROM {{ ref('mart_stage_summary') }}
WHERE moving_seconds > (elapsed_seconds + 1e-4)

UNION ALL

SELECT
    trip_id || ':' || CAST(trip_date AS VARCHAR) AS stage_id,
    total_moving_seconds AS moving_seconds,
    total_elapsed_seconds AS elapsed_seconds
FROM {{ ref('mart_trip_daily_summary') }}
WHERE total_moving_seconds > (total_elapsed_seconds + 1e-4)
