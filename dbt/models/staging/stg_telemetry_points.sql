SELECT
    CAST(trip_id AS VARCHAR) AS trip_id,
    CAST(stage_id AS VARCHAR) AS stage_id,
    CAST(seq AS BIGINT) AS seq,
    CAST(ts_utc AS TIMESTAMPTZ) AS ts_utc,
    CAST(lat AS DOUBLE) AS lat,
    CAST(lon AS DOUBLE) AS lon,
    CAST(elev_raw_m AS DOUBLE) AS elev_raw_m,
    CAST(elev_m AS DOUBLE) AS elev_m,
    CAST(delta_dist_m AS DOUBLE) AS delta_dist_m,
    CAST(cum_dist_m AS DOUBLE) AS cum_dist_m,
    CAST(speed_mps AS DOUBLE) AS speed_mps,
    CAST(grade_pct AS DOUBLE) AS grade_pct,
    CAST(hr_bpm AS INTEGER) AS hr_bpm,
    CAST(cadence AS INTEGER) AS cadence,
    CAST(watts AS INTEGER) AS watts,
    CAST(segment_idx AS INTEGER) AS segment_idx,
    CAST(is_moving AS BOOLEAN) AS is_moving,
    COALESCE(CAST(is_trimmed AS BOOLEAN), FALSE) AS is_trimmed,
    geometry
FROM read_parquet('{{ var("silver_parquet_glob", "../data/silver/**/*.parquet") }}', union_by_name=true)
WHERE NOT COALESCE(is_trimmed, FALSE)
  AND lat BETWEEN -90.0 AND 90.0
  AND lon BETWEEN -180.0 AND 180.0
