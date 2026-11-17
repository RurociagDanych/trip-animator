WITH stage_geo AS (
    SELECT
        m.trip_id,
        m.stage_id,
        EXTRACT(YEAR FROM m.started_at_local_date)::INTEGER AS year,
        m.point_count,
        m.total_distance_m,
        m.elevation_gain_m,
        m.elevation_loss_m,
        m.moving_seconds,
        m.elapsed_seconds,
        COALESCE(g.countries, CAST([] AS VARCHAR[])) AS stage_countries
    FROM {{ ref('mart_stage_summary') }} AS m
    LEFT JOIN {{ ref('mart_stage_geo_context') }} AS g ON g.stage_id = m.stage_id
),
exploded_countries AS (
    SELECT
        sg.trip_id,
        sg.year,
        UNNEST(sg.stage_countries) AS country_name
    FROM stage_geo AS sg
),
country_lists AS (
    SELECT
        trip_id,
        year,
        LIST(DISTINCT country_name ORDER BY country_name ASC) AS countries
    FROM exploded_countries
    GROUP BY trip_id, year
)
SELECT
    sg.trip_id,
    sg.year,
    COUNT(*)::INTEGER AS stage_count,
    SUM(sg.point_count)::BIGINT AS total_points,
    SUM(sg.total_distance_m)::DOUBLE AS total_distance_m,
    SUM(sg.elevation_gain_m)::DOUBLE AS total_elevation_gain_m,
    SUM(sg.elevation_loss_m)::DOUBLE AS total_elevation_loss_m,
    SUM(sg.moving_seconds)::DOUBLE AS total_moving_seconds,
    SUM(sg.elapsed_seconds)::DOUBLE AS total_elapsed_seconds,
    COALESCE(cl.countries, CAST([] AS VARCHAR[])) AS countries
FROM stage_geo AS sg
LEFT JOIN country_lists AS cl
  ON cl.trip_id = sg.trip_id
 AND cl.year = sg.year
GROUP BY sg.trip_id, sg.year, cl.countries
