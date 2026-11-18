-- Pure SQL + DuckDB spatial hex-bin coverage grid (~1.1 km cells, zero community extensions).
WITH pts_with_year AS (
    SELECT
        p.trip_id,
        p.stage_id,
        EXTRACT(YEAR FROM m.started_at_local_date)::INTEGER AS year,
        p.lat,
        p.lon,
        ROUND(p.lat / 0.01)::BIGINT AS q_idx,
        ROUND(
            (p.lon / (0.01 * GREATEST(COS(RADIANS(p.lat)), 0.2)))
            + CASE WHEN ABS(ROUND(p.lat / 0.01))::BIGINT % 2 = 1 THEN 0.5 ELSE 0.0 END
        )::BIGINT AS r_idx
    FROM {{ ref('stg_telemetry_points') }} AS p
    JOIN {{ ref('mart_stage_summary') }} AS m ON m.stage_id = p.stage_id
)
SELECT
    trip_id,
    stage_id,
    year,
    'hex_' || CAST(q_idx AS VARCHAR) || '_' || CAST(r_idx AS VARCHAR) AS h3_index,
    ROUND(AVG(lat), 5)::DOUBLE AS center_lat,
    ROUND(AVG(lon), 5)::DOUBLE AS center_lon,
    COUNT(*)::BIGINT AS point_count,
    1::INTEGER AS stage_count
FROM pts_with_year
GROUP BY trip_id, stage_id, year, q_idx, r_idx
