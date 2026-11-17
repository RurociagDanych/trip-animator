-- Singular test: started_at_utc must be <= ended_at_utc.
SELECT
    stage_id,
    started_at_utc,
    ended_at_utc
FROM {{ ref('mart_stage_summary') }}
WHERE started_at_utc IS NOT NULL
  AND ended_at_utc IS NOT NULL
  AND started_at_utc > ended_at_utc
