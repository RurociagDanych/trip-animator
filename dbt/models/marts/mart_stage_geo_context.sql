WITH stage_pts AS (
    SELECT
        trip_id,
        stage_id,
        lat,
        lon,
        ROW_NUMBER() OVER (PARTITION BY stage_id ORDER BY seq ASC) AS rn,
        COUNT(*) OVER (PARTITION BY stage_id) AS total_n
    FROM {{ ref('stg_telemetry_points') }}
),
sampled AS (
    SELECT trip_id, stage_id, lat, lon
    FROM stage_pts
    WHERE total_n <= 200 OR (rn % GREATEST((total_n / 200)::BIGINT, 1)) = 1
),
countries_ref AS (
    SELECT name, min_lat, max_lat, min_lon, max_lon, geometry
    FROM read_parquet('{{ var("ref_data_dir", "../data/ref") }}/countries.parquet')
),
regions_ref AS (
    SELECT name, min_lat, max_lat, min_lon, max_lon, geometry
    FROM read_parquet('{{ var("ref_data_dir", "../data/ref") }}/admin_regions.parquet')
),
passes_ref AS (
    SELECT name, lat, lon
    FROM read_parquet('{{ var("ref_data_dir", "../data/ref") }}/mountain_passes.parquet')
),
stage_countries AS (
    SELECT
        s.stage_id,
        LIST(DISTINCT c.name ORDER BY c.name ASC) AS countries
    FROM sampled AS s
    JOIN countries_ref AS c
      ON s.lat BETWEEN c.min_lat AND c.max_lat
     AND s.lon BETWEEN c.min_lon AND c.max_lon
     AND ST_Intersects(c.geometry, ST_Point(s.lon, s.lat))
    GROUP BY s.stage_id
),
stage_regions AS (
    SELECT
        s.stage_id,
        LIST(DISTINCT r.name ORDER BY r.name ASC) AS regions
    FROM sampled AS s
    JOIN regions_ref AS r
      ON s.lat BETWEEN r.min_lat AND r.max_lat
     AND s.lon BETWEEN r.min_lon AND r.max_lon
     AND ST_Intersects(r.geometry, ST_Point(s.lon, s.lat))
    GROUP BY s.stage_id
),
stage_passes AS (
    SELECT
        s.stage_id,
        LIST(DISTINCT p.name ORDER BY p.name ASC) AS passes
    FROM sampled AS s
    JOIN passes_ref AS p
      ON ABS(p.lat - s.lat) <= 0.025
     AND ABS(p.lon - s.lon) <= 0.035
    GROUP BY s.stage_id
)
SELECT
    m.stage_id,
    m.trip_id,
    COALESCE(sc.countries, CAST([] AS VARCHAR[])) AS countries,
    sc.countries[1] AS primary_country,
    COALESCE(sr.regions, CAST([] AS VARCHAR[])) AS regions,
    COALESCE(sp.passes, CAST([] AS VARCHAR[])) AS passes
FROM {{ ref('mart_stage_summary') }} AS m
LEFT JOIN stage_countries AS sc ON sc.stage_id = m.stage_id
LEFT JOIN stage_regions AS sr ON sr.stage_id = m.stage_id
LEFT JOIN stage_passes AS sp ON sp.stage_id = m.stage_id
