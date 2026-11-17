WITH flagged AS (
    SELECT
        trip_id,
        stage_id,
        seq,
        segment_idx,
        ts_utc,
        lat,
        lon,
        CASE
            WHEN NOT COALESCE(is_moving, TRUE) OR speed_mps < 0.5 THEN 1
            ELSE 0
        END AS is_dwell
    FROM {{ ref('stg_telemetry_points') }}
    WHERE ts_utc IS NOT NULL
),
islands AS (
    SELECT
        *,
        SUM(CASE WHEN is_dwell = 0 THEN 1 ELSE 0 END) OVER (
            PARTITION BY trip_id, stage_id, segment_idx ORDER BY seq
        ) AS dwell_grp
    FROM flagged
),
clusters AS (
    SELECT
        trip_id,
        stage_id,
        segment_idx,
        dwell_grp,
        AVG(lat)::DOUBLE AS lat,
        AVG(lon)::DOUBLE AS lon,
        MIN(ts_utc) AS started_at_utc,
        MAX(ts_utc) AS ended_at_utc,
        (EPOCH(MAX(ts_utc)) - EPOCH(MIN(ts_utc)))::DOUBLE AS duration_s
    FROM islands
    WHERE is_dwell = 1
    GROUP BY trip_id, stage_id, segment_idx, dwell_grp
    HAVING (EPOCH(MAX(ts_utc)) - EPOCH(MIN(ts_utc))) >= 180.0
)
SELECT
    stage_id || ':dwell:' || CAST(ROW_NUMBER() OVER (PARTITION BY stage_id ORDER BY started_at_utc ASC) AS VARCHAR) AS dwell_id,
    trip_id,
    stage_id,
    ROUND(lat, 6)::DOUBLE AS lat,
    ROUND(lon, 6)::DOUBLE AS lon,
    started_at_utc,
    ended_at_utc,
    ROUND(duration_s, 1)::DOUBLE AS duration_s,
    CASE
        WHEN duration_s >= 14400.0 THEN 'tent'
        WHEN duration_s >= 1800.0 AND (EXTRACT(HOUR FROM started_at_utc) BETWEEN 11 AND 14 OR EXTRACT(HOUR FROM started_at_utc) BETWEEN 18 AND 21) THEN 'meal'
        ELSE 'coffee'
    END AS suggested_icon
FROM clusters
