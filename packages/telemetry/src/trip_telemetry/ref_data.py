"""Compact static GeoParquet reference tables (countries, admin regions, mountain passes)."""

from __future__ import annotations

import json
import os
import struct
import tempfile
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]


def _wkb_point(lon: float, lat: float) -> bytes:
    return struct.pack("<BIdd", 1, 1, float(lon), float(lat))


def _wkb_polygon(ring: list[tuple[float, float]]) -> bytes:
    closed = list(ring)
    if closed[0] != closed[-1]:
        closed.append(closed[0])
    parts = [struct.pack("<BIII", 1, 3, 1, len(closed))]
    for lon, lat in closed:
        parts.append(struct.pack("<dd", float(lon), float(lat)))
    return b"".join(parts)


def _bbox_polygon(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> list[tuple[float, float]]:
    return [
        (min_lon, min_lat),
        (max_lon, min_lat),
        (max_lon, max_lat),
        (min_lon, max_lat),
        (min_lon, min_lat),
    ]


# Simplified non-overlapping / prioritized country polygons (lon, lat)
_COUNTRIES: list[tuple[str, str, str, list[tuple[float, float]]]] = [
    ("CH", "CHE", "Switzerland", [(5.95, 45.81), (10.49, 45.81), (10.49, 47.81), (5.95, 47.81)]),
    ("LI", "LIE", "Liechtenstein", [(9.47, 47.04), (9.64, 47.04), (9.64, 47.27), (9.47, 47.27)]),
    ("AD", "AND", "Andorra", [(1.40, 42.42), (1.79, 42.42), (1.79, 42.66), (1.40, 42.66)]),
    (
        "IT",
        "ITA",
        "Italy",
        [
            (6.62, 44.0),
            (7.90, 45.80),
            (10.50, 45.80),
            (10.50, 47.09),
            (13.90, 47.09),
            (13.90, 45.55),
            (18.52, 40.00),
            (16.00, 37.90),
            (12.30, 36.65),
            (8.10, 38.80),
            (8.10, 41.30),
            (6.62, 44.0),
        ],
    ),
    (
        "FR",
        "FRA",
        "France",
        [
            (-4.80, 43.30),
            (3.20, 42.33),
            (7.60, 43.75),
            (5.95, 45.81),
            (5.95, 47.60),
            (8.23, 48.95),
            (4.20, 51.09),
            (-1.90, 49.70),
            (-4.80, 48.40),
        ],
    ),
    (
        "AT",
        "AUT",
        "Austria",
        [(9.53, 46.37), (17.16, 46.37), (17.16, 49.02), (12.10, 49.02), (9.53, 47.56)],
    ),
    (
        "DE",
        "DEU",
        "Germany",
        [(5.87, 47.27), (13.84, 47.27), (15.04, 51.10), (14.12, 54.90), (5.87, 55.06)],
    ),
    (
        "ES",
        "ESP",
        "Spain",
        [
            (-9.30, 36.00),
            (4.33, 36.00),
            (4.33, 42.90),
            (1.40, 42.42),
            (-1.80, 43.40),
            (-9.30, 43.79),
        ],
    ),
    ("PT", "PRT", "Portugal", [(-9.50, 36.96), (-6.19, 36.96), (-6.19, 42.15), (-9.50, 42.15)]),
    ("PL", "POL", "Poland", [(14.12, 49.00), (24.15, 49.00), (24.15, 54.84), (14.12, 54.84)]),
    ("SK", "SVK", "Slovakia", [(16.83, 47.73), (22.57, 47.73), (22.57, 49.61), (16.83, 49.61)]),
    ("SI", "SVN", "Slovenia", [(13.38, 45.42), (16.61, 45.42), (16.61, 46.88), (13.38, 46.88)]),
    ("CZ", "CZE", "Czechia", [(12.09, 48.55), (18.86, 48.55), (18.86, 51.06), (12.09, 51.06)]),
    ("BE", "BEL", "Belgium", [(2.54, 49.50), (6.41, 49.50), (6.41, 51.50), (2.54, 51.50)]),
    ("NL", "NLD", "Netherlands", [(3.36, 50.75), (7.23, 50.75), (7.23, 53.55), (3.36, 53.55)]),
    ("GB", "GBR", "United Kingdom", [(-8.65, 49.86), (1.77, 49.86), (1.77, 60.86), (-8.65, 60.86)]),
    ("NO", "NOR", "Norway", [(4.50, 57.95), (31.17, 57.95), (31.17, 71.19), (4.50, 71.19)]),
    ("SE", "SWE", "Sweden", [(10.96, 55.34), (24.17, 55.34), (24.17, 69.06), (10.96, 69.06)]),
    ("HR", "HRV", "Croatia", [(13.49, 42.39), (19.43, 42.39), (19.43, 46.55), (13.49, 46.55)]),
    ("GR", "GRC", "Greece", [(19.37, 34.80), (29.65, 34.80), (29.65, 41.75), (19.37, 41.75)]),
    ("IS", "ISL", "Iceland", [(-24.53, 63.39), (-13.49, 63.39), (-13.49, 66.54), (-24.53, 66.54)]),
    ("US", "USA", "United States", [(-168.0, 18.9), (-66.9, 18.9), (-66.9, 71.4), (-168.0, 71.4)]),
    ("CA", "CAN", "Canada", [(-141.0, 41.68), (-52.62, 41.68), (-52.62, 83.11), (-141.0, 83.11)]),
    (
        "CL",
        "CHL",
        "Chile",
        [(-75.64, -55.98), (-66.42, -55.98), (-66.42, -17.50), (-75.64, -17.50)],
    ),
    (
        "AR",
        "ARG",
        "Argentina",
        [(-73.56, -55.06), (-53.64, -55.06), (-53.64, -21.78), (-73.56, -21.78)],
    ),
    ("PE", "PER", "Peru", [(-81.33, -18.35), (-68.65, -18.35), (-68.65, -0.04), (-81.33, -0.04)]),
    ("MA", "MAR", "Morocco", [(-13.17, 27.67), (-0.99, 27.67), (-0.99, 35.92), (-13.17, 35.92)]),
    (
        "ZA",
        "ZAF",
        "South Africa",
        [(16.45, -34.83), (32.89, -34.83), (32.89, -22.13), (16.45, -22.13)],
    ),
    ("JP", "JPN", "Japan", [(122.93, 24.04), (153.99, 24.04), (153.99, 45.55), (122.93, 45.55)]),
    ("NP", "NPL", "Nepal", [(80.06, 26.35), (88.20, 26.35), (88.20, 30.45), (80.06, 30.45)]),
    (
        "NZ",
        "NZL",
        "New Zealand",
        [(166.43, -47.29), (178.58, -47.29), (178.58, -34.39), (166.43, -34.39)],
    ),
    (
        "AU",
        "AUS",
        "Australia",
        [(112.92, -43.64), (153.64, -43.64), (153.64, -10.67), (112.92, -10.67)],
    ),
]

_ADMIN_REGIONS: list[tuple[str, str, str, str, float, float, float, float]] = [
    ("CH-BE", "Bernese Oberland", "CHE", "Switzerland", 7.20, 46.30, 8.50, 46.85),
    ("CH-VS", "Valais", "CHE", "Switzerland", 6.77, 45.85, 8.48, 46.66),
    ("CH-GR", "Graubunden", "CHE", "Switzerland", 8.65, 46.16, 10.49, 47.07),
    ("CH-TI", "Ticino", "CHE", "Switzerland", 8.38, 45.81, 9.16, 46.63),
    ("CH-UR", "Uri", "CHE", "Switzerland", 8.39, 46.52, 8.96, 46.99),
    ("IT-32", "Trentino-Alto Adige", "ITA", "Italy", 10.37, 45.67, 12.48, 47.09),
    ("IT-34", "Veneto", "ITA", "Italy", 10.62, 44.79, 13.10, 46.68),
    ("IT-25", "Lombardy", "ITA", "Italy", 8.50, 44.68, 11.43, 46.63),
    ("IT-21", "Piedmont", "ITA", "Italy", 6.62, 44.06, 9.21, 46.46),
    ("IT-23", "Aosta Valley", "ITA", "Italy", 6.80, 45.47, 7.95, 45.99),
    ("FR-73", "Savoie", "FRA", "France", 5.62, 45.05, 7.18, 45.94),
    ("FR-74", "Haute-Savoie", "FRA", "France", 5.80, 45.68, 7.05, 46.41),
    ("FR-38", "Isere", "FRA", "France", 4.74, 44.69, 6.36, 45.88),
    ("FR-05", "Hautes-Alpes", "FRA", "France", 5.42, 44.18, 7.08, 45.13),
    ("FR-84", "Vaucluse", "FRA", "France", 4.65, 43.66, 5.76, 44.43),
    ("FR-65", "Hautes-Pyrenees", "FRA", "France", -0.33, 42.68, 0.65, 43.62),
    ("AT-7", "Tyrol", "AUT", "Austria", 10.10, 46.65, 12.97, 47.74),
    ("AT-5", "Salzburg", "AUT", "Austria", 12.08, 46.94, 13.99, 48.02),
    ("AT-2", "Carinthia", "AUT", "Austria", 12.66, 46.37, 15.07, 47.13),
    ("DE-BY", "Bavaria", "DEU", "Germany", 8.97, 47.27, 13.84, 50.56),
    ("PL-12", "Lesser Poland", "POL", "Poland", 19.08, 49.17, 21.42, 50.52),
    ("PL-02", "Lower Silesia", "POL", "Poland", 14.82, 50.09, 17.79, 51.80),
    ("ES-CT", "Catalonia", "ESP", "Spain", 0.16, 40.52, 3.33, 42.86),
    ("ES-AS", "Asturias", "ESP", "Spain", -7.18, 42.88, -4.51, 43.67),
    ("ES-PM", "Balearic Islands", "ESP", "Spain", 1.19, 38.64, 4.33, 40.09),
    ("ES-AN", "Andalusia", "ESP", "Spain", -7.52, 36.00, -1.63, 38.73),
    ("SI-JA", "Julian Alps", "SVN", "Slovenia", 13.45, 46.08, 14.35, 46.55),
    ("NO-INN", "Innlandet", "NOR", "Norway", 7.34, 59.84, 12.87, 62.66),
    ("GB-SCT", "Scottish Highlands", "GBR", "United Kingdom", -6.50, 56.30, -3.00, 58.65),
    ("US-CO", "Colorado", "USA", "United States", -109.06, 36.99, -102.04, 41.00),
    ("US-CA", "California", "USA", "United States", -124.48, 32.53, -114.13, 42.01),
    ("US-UT", "Utah", "USA", "United States", -114.05, 37.00, -109.04, 42.00),
    ("CA-BC", "British Columbia", "CAN", "Canada", -139.06, 48.30, -114.05, 60.00),
    ("JP-22", "Shizuoka", "JPN", "Japan", 137.47, 34.57, 139.18, 35.65),
    ("NP-GA", "Gandaki", "NPL", "Nepal", 82.88, 27.45, 85.20, 29.33),
]

# (pass_id, name, elev_m, country_iso, region_name, lat, lon)
_MOUNTAIN_PASSES: list[tuple[str, str, float, str, str, float, float]] = [
    ("pass-stelvio", "Passo dello Stelvio", 2758.0, "ITA", "Lombardy", 46.5286, 10.4531),
    ("pass-giau", "Passo Giau", 2236.0, "ITA", "Veneto", 46.4828, 12.0542),
    ("pass-pordoi", "Passo Pordoi", 2239.0, "ITA", "Trentino-Alto Adige", 46.4875, 11.8125),
    ("pass-gardena", "Passo Gardena", 2121.0, "ITA", "Trentino-Alto Adige", 46.5494, 11.8086),
    ("pass-sella", "Passo Sella", 2218.0, "ITA", "Trentino-Alto Adige", 46.5089, 11.7586),
    ("pass-campolongo", "Passo Campolongo", 1875.0, "ITA", "Veneto", 46.5169, 11.8742),
    ("pass-falzarego", "Passo Falzarego", 2105.0, "ITA", "Veneto", 46.5194, 12.0094),
    ("pass-valparola", "Passo Valparola", 2192.0, "ITA", "Veneto", 46.5356, 11.9886),
    ("pass-fedaia", "Passo Fedaia", 2057.0, "ITA", "Trentino-Alto Adige", 46.4539, 11.8889),
    ("pass-tre-cime", "Rifugio Auronzo (Tre Cime)", 2320.0, "ITA", "Veneto", 46.6122, 12.2958),
    ("pass-gavia", "Passo Gavia", 2621.0, "ITA", "Lombardy", 46.3436, 10.4875),
    ("pass-mortirolo", "Passo del Mortirolo", 1852.0, "ITA", "Lombardy", 46.2481, 10.2994),
    ("pass-bernina", "Passo del Bernina", 2328.0, "CHE", "Graubunden", 46.4108, 10.0278),
    ("pass-spluga", "Passo dello Spluga", 2114.0, "CHE", "Graubunden", 46.5056, 9.3303),
    ("pass-maloja", "Malojapass", 1815.0, "CHE", "Graubunden", 46.4000, 9.6956),
    ("pass-julier", "Julierpass", 2284.0, "CHE", "Graubunden", 46.4722, 9.7278),
    ("pass-albula", "Albulapass", 2312.0, "CHE", "Graubunden", 46.5825, 9.8375),
    ("pass-fluela", "Fluelapass", 2383.0, "CHE", "Graubunden", 46.7503, 9.9475),
    ("pass-oberalp", "Oberalppass", 2044.0, "CHE", "Graubunden", 46.6589, 8.6714),
    ("pass-furka", "Furkapass", 2429.0, "CHE", "Valais", 46.5725, 8.4153),
    ("pass-grimsel", "Grimselpass", 2164.0, "CHE", "Bernese Oberland", 46.5614, 8.3389),
    ("pass-susten", "Sustenpass", 2224.0, "CHE", "Bernese Oberland", 46.7297, 8.4481),
    ("pass-gotthard", "Gotthardpass", 2106.0, "CHE", "Ticino", 46.5592, 8.5611),
    ("pass-nufenen", "Nufenenpass", 2478.0, "CHE", "Valais", 46.4781, 8.3872),
    ("pass-kl-scheidegg", "Kleine Scheidegg", 2061.0, "CHE", "Bernese Oberland", 46.5853, 7.9611),
    (
        "pass-grosse-scheidegg",
        "Grosse Scheidegg",
        1962.0,
        "CHE",
        "Bernese Oberland",
        46.6558,
        8.1019,
    ),
    ("pass-lauterbrunnen-alps", "Mannlichen", 2225.0, "CHE", "Bernese Oberland", 46.6131, 7.9414),
    ("pass-col-croix", "Col de la Croix", 1776.0, "CHE", "Valais", 46.3228, 7.1275),
    ("pass-gr-st-bernard", "Col du Grand-Saint-Bernard", 2469.0, "CHE", "Valais", 45.8689, 7.1706),
    ("pass-finestre", "Colle delle Finestre", 2178.0, "ITA", "Piedmont", 45.0722, 7.0531),
    ("pass-agnello", "Colle dell'Agnello", 2744.0, "ITA", "Piedmont", 44.6836, 6.9794),
    ("pass-fauniera", "Colle Fauniera", 2481.0, "ITA", "Piedmont", 44.3858, 7.1219),
    ("pass-nivolet", "Colle del Nivolet", 2612.0, "ITA", "Piedmont", 45.4792, 7.1436),
    ("pass-galibier", "Col du Galibier", 2642.0, "FRA", "Savoie", 45.0642, 6.4078),
    ("pass-alpe-dhuez", "Alpe d'Huez", 1850.0, "FRA", "Isere", 45.0919, 6.0706),
    ("pass-croix-fer", "Col de la Croix de Fer", 2067.0, "FRA", "Savoie", 45.2275, 6.2033),
    ("pass-glandon", "Col du Glandon", 1924.0, "FRA", "Savoie", 45.2397, 6.1756),
    ("pass-telegraphe", "Col du Telegraphe", 1566.0, "FRA", "Savoie", 45.2025, 6.4444),
    ("pass-madeleine", "Col de la Madeleine", 1993.0, "FRA", "Savoie", 45.4347, 6.3750),
    ("pass-iseran", "Col de l'Iseran", 2770.0, "FRA", "Savoie", 45.4169, 7.0308),
    ("pass-zoard", "Col d'Izoard", 2360.0, "FRA", "Hautes-Alpes", 44.8200, 6.7350),
    ("pass-vars", "Col de Vars", 2108.0, "FRA", "Hautes-Alpes", 44.5389, 6.7028),
    ("pass-bonette", "Col de la Bonette", 2802.0, "FRA", "Hautes-Alpes", 44.3267, 6.8075),
    ("pass-ventoux", "Mont Ventoux", 1909.0, "FRA", "Vaucluse", 44.1742, 5.2789),
    ("pass-colombiere", "Col de la Colombiere", 1613.0, "FRA", "Haute-Savoie", 45.9922, 6.4758),
    ("pass-joux-plane", "Col de Joux Plane", 1691.0, "FRA", "Haute-Savoie", 46.1306, 6.7108),
    ("pass-tourmalet", "Col du Tourmalet", 2115.0, "FRA", "Hautes-Pyrenees", 42.9083, 0.1453),
    ("pass-aubisque", "Col d'Aubisque", 1709.0, "FRA", "Hautes-Pyrenees", 42.9767, -0.3394),
    ("pass-aspin", "Col d'Aspin", 1489.0, "FRA", "Hautes-Pyrenees", 42.9422, 0.3269),
    ("pass-peyresourde", "Col de Peyresourde", 1569.0, "FRA", "Hautes-Pyrenees", 42.7967, 0.4458),
    ("pass-grossglockner", "Hochtor (Grossglockner)", 2504.0, "AUT", "Carinthia", 47.0825, 12.8425),
    ("pass-timmelsjoch", "Timmelsjoch", 2474.0, "AUT", "Tyrol", 46.9053, 11.0972),
    ("pass-retbach", "Rettenbachferner", 2796.0, "AUT", "Tyrol", 46.9258, 10.9464),
    ("pass-kaunertal", "Kaunertal Glacier", 2750.0, "AUT", "Tyrol", 46.8639, 10.7131),
    ("pass-vrsic", "Vrsic Pass", 1611.0, "SVN", "Julian Alps", 46.4339, 13.7436),
    ("pass-mangart", "Mangart Saddle", 2055.0, "SVN", "Julian Alps", 46.4408, 13.6431),
    ("pass-kasprowy", "Kasprowy Wierch", 1987.0, "POL", "Lesser Poland", 49.2317, 19.9817),
    (
        "pass-morskie-oko",
        "Rysy / Czarny Staw Ridge",
        1583.0,
        "POL",
        "Lesser Poland",
        49.1886,
        20.0758,
    ),
    ("pass-karkonoska", "Przelecz Karkonoska", 1198.0, "POL", "Lower Silesia", 50.7594, 15.6392),
    ("pass-angliru", "Alto de l'Angliru", 1570.0, "ESP", "Asturias", 43.2214, -5.9419),
    ("pass-lagos", "Lagos de Covadonga", 1134.0, "ESP", "Asturias", 43.2708, -4.9875),
    (
        "pass-sa-calobra",
        "Coll dels Reis (Sa Calobra)",
        682.0,
        "ESP",
        "Balearic Islands",
        39.8322,
        2.8150,
    ),
    ("pass-veleta", "Pico del Veleta", 3396.0, "ESP", "Andalusia", 37.0561, -3.3656),
    ("pass-rocacorba", "Rocacorba", 985.0, "ESP", "Catalonia", 42.0689, 2.6875),
    ("pass-bealach", "Bealach na Ba", 626.0, "GBR", "Scottish Highlands", 57.4125, -5.7022),
    ("pass-trollstigen", "Trollstigen", 850.0, "NOR", "Innlandet", 62.4536, 7.6639),
    ("pass-evans", "Mount Blue Sky", 4307.0, "USA", "Colorado", 39.5883, -105.6438),
    ("pass-independence", "Independence Pass", 3687.0, "USA", "Colorado", 39.1086, -106.5642),
    ("pass-tioga", "Tioga Pass", 3031.0, "USA", "California", 37.9108, -119.2578),
    ("pass-whitney-portal", "Whitney Portal", 2548.0, "USA", "California", 36.5872, -118.2397),
    ("pass-fuji", "Mount Fuji 5th Station", 2305.0, "JPN", "Shizuoka", 35.3944, 138.7322),
    ("pass-thorong-la", "Thorong La Pass", 5416.0, "NPL", "Gandaki", 28.7936, 83.9372),
]


def _write_geoparquet(table: pa.Table, path: Path, geom_type: str, bbox: list[float]) -> Path:
    geo_meta = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": [geom_type],
                "bbox": bbox,
            }
        },
    }
    meta = dict(table.schema.metadata or {})
    meta[b"geo"] = json.dumps(geo_meta).encode("utf-8")
    table = table.replace_schema_metadata(meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return path


def build_reference_parquets(ref_dir: Path) -> Path:
    """Materialize compact static GeoParquet reference tables into `ref_dir`."""
    ref_dir.mkdir(parents=True, exist_ok=True)

    c_iso2, c_iso3, c_names, c_min_lon, c_min_lat, c_max_lon, c_max_lat, c_geom = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    for iso2, iso3, name, ring in _COUNTRIES:
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        c_iso2.append(iso2)
        c_iso3.append(iso3)
        c_names.append(name)
        c_min_lon.append(min(lons))
        c_min_lat.append(min(lats))
        c_max_lon.append(max(lons))
        c_max_lat.append(max(lats))
        c_geom.append(_wkb_polygon(ring))

    countries_table = pa.table(
        {
            "iso_a2": pa.array(c_iso2, type=pa.string()),
            "iso_a3": pa.array(c_iso3, type=pa.string()),
            "name": pa.array(c_names, type=pa.string()),
            "min_lon": pa.array(c_min_lon, type=pa.float64()),
            "min_lat": pa.array(c_min_lat, type=pa.float64()),
            "max_lon": pa.array(c_max_lon, type=pa.float64()),
            "max_lat": pa.array(c_max_lat, type=pa.float64()),
            "geometry": pa.array(c_geom, type=pa.binary()),
        }
    )
    _write_geoparquet(
        countries_table,
        ref_dir / "countries.parquet",
        "Polygon",
        [min(c_min_lon), min(c_min_lat), max(c_max_lon), max(c_max_lat)],
    )

    r_code, r_name, r_ciso, r_cname, r_min_lon, r_min_lat, r_max_lon, r_max_lat, r_geom = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    for code, name, c_iso, c_name, min_lon, min_lat, max_lon, max_lat in _ADMIN_REGIONS:
        r_code.append(code)
        r_name.append(name)
        r_ciso.append(c_iso)
        r_cname.append(c_name)
        r_min_lon.append(min_lon)
        r_min_lat.append(min_lat)
        r_max_lon.append(max_lon)
        r_max_lat.append(max_lat)
        r_geom.append(_wkb_polygon(_bbox_polygon(min_lon, min_lat, max_lon, max_lat)))

    regions_table = pa.table(
        {
            "region_code": pa.array(r_code, type=pa.string()),
            "name": pa.array(r_name, type=pa.string()),
            "country_iso": pa.array(r_ciso, type=pa.string()),
            "country_name": pa.array(r_cname, type=pa.string()),
            "min_lon": pa.array(r_min_lon, type=pa.float64()),
            "min_lat": pa.array(r_min_lat, type=pa.float64()),
            "max_lon": pa.array(r_max_lon, type=pa.float64()),
            "max_lat": pa.array(r_max_lat, type=pa.float64()),
            "geometry": pa.array(r_geom, type=pa.binary()),
        }
    )
    _write_geoparquet(
        regions_table,
        ref_dir / "admin_regions.parquet",
        "Polygon",
        [min(r_min_lon), min(r_min_lat), max(r_max_lon), max(r_max_lat)],
    )

    p_id, p_name, p_elev, p_ciso, p_reg, p_lat, p_lon, p_geom = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    for pid, name, elev_m, c_iso, reg, lat, lon in _MOUNTAIN_PASSES:
        p_id.append(pid)
        p_name.append(name)
        p_elev.append(elev_m)
        p_ciso.append(c_iso)
        p_reg.append(reg)
        p_lat.append(lat)
        p_lon.append(lon)
        p_geom.append(_wkb_point(lon, lat))

    passes_table = pa.table(
        {
            "pass_id": pa.array(p_id, type=pa.string()),
            "name": pa.array(p_name, type=pa.string()),
            "elev_m": pa.array(p_elev, type=pa.float64()),
            "country_iso": pa.array(p_ciso, type=pa.string()),
            "region_name": pa.array(p_reg, type=pa.string()),
            "lat": pa.array(p_lat, type=pa.float64()),
            "lon": pa.array(p_lon, type=pa.float64()),
            "geometry": pa.array(p_geom, type=pa.binary()),
        }
    )
    _write_geoparquet(
        passes_table,
        ref_dir / "mountain_passes.parquet",
        "Point",
        [min(p_lon), min(p_lat), max(p_lon), max(p_lat)],
    )

    return ref_dir


_REF_FILES = ("countries.parquet", "admin_regions.parquet", "mountain_passes.parquet")


def _has_ref_files(ref_dir: Path) -> bool:
    return all((ref_dir / name).is_file() for name in _REF_FILES)


def _ensure_ref_dir_or_tmp(candidate: Path, *, ensure_exists: bool) -> Path:
    if not ensure_exists or _has_ref_files(candidate):
        return candidate
    try:
        return build_reference_parquets(candidate)
    except OSError:
        tmp_fallback = Path(tempfile.gettempdir()) / "trip_ref"
        if not _has_ref_files(tmp_fallback):
            build_reference_parquets(tmp_fallback)
        return tmp_fallback


def resolve_ref_data_dir(ref_dir: Path | None = None, *, ensure_exists: bool = True) -> Path:
    """Resolve `data/ref` directory, materializing compact ref tables if absent."""
    if ref_dir is not None:
        resolved = Path(ref_dir).resolve()
        return _ensure_ref_dir_or_tmp(resolved, ensure_exists=ensure_exists)

    env_dir = os.environ.get("TRIP_REF_DATA_DIR")
    if env_dir:
        candidate = Path(env_dir).resolve()
        return _ensure_ref_dir_or_tmp(candidate, ensure_exists=ensure_exists)

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "dbt").is_dir():
            candidate = parent / "data" / "ref"
            return _ensure_ref_dir_or_tmp(candidate, ensure_exists=ensure_exists)

    fallback = Path.cwd() / "data" / "ref"
    return _ensure_ref_dir_or_tmp(fallback, ensure_exists=ensure_exists)
