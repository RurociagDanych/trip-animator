-- depends_on: {{ ref('mart_stage_summary') }}
{{
    config(
        materialized='incremental',
        unique_key='stage_id',
        incremental_strategy='delete+insert',
        pre_hook="{% if is_incremental() %} DELETE FROM {{ this }} WHERE stage_id NOT IN (SELECT stage_id FROM {{ ref('mart_stage_summary') }}) OR stage_id IN (SELECT stage_id FROM {{ ref('mart_stage_summary') }} WHERE _dbt_invocation_id = '{{ invocation_id }}'); {% endif %}"
    )
}}
WITH base AS (
    SELECT
        trip_id,
        stage_id,
        seq,
        segment_idx,
        lat,
        lon,
        elev_m,
        cum_dist_m,
        grade_pct,
        cum_dist_m - FIRST_VALUE(cum_dist_m) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS rel_dist_m,
        LAG(segment_idx) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_seg,
        LAG(elev_m) OVER (PARTITION BY trip_id, stage_id ORDER BY seq) AS prev_elev_m
    FROM {{ ref('stg_telemetry_points') }}
    {% if is_incremental() %}
    WHERE stage_id IN (
        SELECT stage_id
        FROM {{ ref('mart_stage_summary') }}
        WHERE _dbt_invocation_id = '{{ invocation_id }}'
    )
    {% endif %}
),
flagged AS (
    SELECT
        *,
        CASE
            WHEN prev_seg = segment_idx
                 AND elev_m IS NOT NULL
                 AND prev_elev_m IS NOT NULL
                 AND COALESCE(grade_pct, 0.0) >= 3.0
            THEN 1
            ELSE 0
        END AS is_climb_pt,
        CASE
            WHEN prev_seg = segment_idx AND elev_m IS NOT NULL AND prev_elev_m IS NOT NULL
            THEN GREATEST(elev_m - prev_elev_m, 0.0)
            ELSE 0.0
        END AS pos_elev_m
    FROM base
),
islands AS (
    SELECT
        *,
        SUM(CASE WHEN is_climb_pt = 0 THEN 1 ELSE 0 END) OVER (
            PARTITION BY trip_id, stage_id ORDER BY seq
        ) AS island_grp
    FROM flagged
),
climb_runs AS (
    SELECT
        trip_id,
        stage_id,
        island_grp,
        MIN(rel_dist_m)::DOUBLE AS start_dist_m,
        MAX(rel_dist_m)::DOUBLE AS end_dist_m,
        (MAX(rel_dist_m) - MIN(rel_dist_m))::DOUBLE AS distance_m,
        SUM(pos_elev_m)::DOUBLE AS elevation_gain_m,
        MAX(elev_m)::DOUBLE AS summit_elev_m,
        MAX(grade_pct)::DOUBLE AS max_grade_pct,
        ARG_MAX(lat, elev_m)::DOUBLE AS summit_lat,
        ARG_MAX(lon, elev_m)::DOUBLE AS summit_lon
    FROM islands
    WHERE is_climb_pt = 1
    GROUP BY trip_id, stage_id, island_grp
    HAVING (MAX(rel_dist_m) - MIN(rel_dist_m)) >= 500.0
       AND SUM(pos_elev_m) >= 30.0
),
scored AS (
    SELECT
        trip_id,
        stage_id,
        ROW_NUMBER() OVER (PARTITION BY stage_id ORDER BY start_dist_m ASC) AS climb_idx,
        start_dist_m,
        end_dist_m,
        distance_m,
        elevation_gain_m,
        summit_elev_m,
        summit_lat,
        summit_lon,
        (elevation_gain_m / NULLIF(distance_m, 0.0)) * 100.0 AS avg_grade_pct,
        COALESCE(max_grade_pct, (elevation_gain_m / NULLIF(distance_m, 0.0)) * 100.0) AS max_grade_pct,
        (distance_m / 1000.0) * POWER((elevation_gain_m / NULLIF(distance_m, 0.0)) * 100.0, 2) AS fiets_score
    FROM climb_runs
),
passes AS (
    SELECT name, lat, lon
    FROM read_parquet('{{ var("ref_data_dir", "../data/ref") }}/mountain_passes.parquet')
)
SELECT
    s.stage_id || ':climb:' || CAST(s.climb_idx AS VARCHAR) AS climb_id,
    s.trip_id,
    s.stage_id,
    COALESCE(
        (
            SELECT p.name
            FROM passes AS p
            WHERE ABS(p.lat - s.summit_lat) <= 0.025
              AND ABS(p.lon - s.summit_lon) <= 0.035
            ORDER BY POWER(p.lat - s.summit_lat, 2) + POWER(p.lon - s.summit_lon, 2) ASC
            LIMIT 1
        ),
        'Climb ' || CAST(s.climb_idx AS VARCHAR) || ' (' || CAST(ROUND(s.summit_elev_m) AS VARCHAR) || 'm)'
    ) AS name,
    CASE
        WHEN s.fiets_score >= 600.0 THEN 'HC'
        WHEN s.fiets_score >= 300.0 THEN 'Cat 1'
        WHEN s.fiets_score >= 150.0 THEN 'Cat 2'
        WHEN s.fiets_score >= 75.0 THEN 'Cat 3'
        ELSE 'Cat 4'
    END AS category,
    ROUND(s.start_dist_m, 1)::DOUBLE AS start_dist_m,
    ROUND(s.end_dist_m, 1)::DOUBLE AS end_dist_m,
    ROUND(s.distance_m, 1)::DOUBLE AS distance_m,
    ROUND(s.elevation_gain_m, 1)::DOUBLE AS elevation_gain_m,
    ROUND(s.avg_grade_pct, 2)::DOUBLE AS avg_grade_pct,
    ROUND(s.max_grade_pct, 2)::DOUBLE AS max_grade_pct,
    ROUND(s.fiets_score, 2)::DOUBLE AS fiets_score,
    ROUND(s.summit_lat, 6)::DOUBLE AS summit_lat,
    ROUND(s.summit_lon, 6)::DOUBLE AS summit_lon,
    ROUND(s.summit_elev_m, 1)::DOUBLE AS summit_elev_m
FROM scored AS s
