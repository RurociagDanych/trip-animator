"""Unit, property-based (Hypothesis), and brute-force oracle tests for trip_telemetry.tiles."""

from __future__ import annotations

import math
from collections import deque

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from trip_telemetry import (
    DEFAULT_EXPLORER_ZOOM,
    MAX_MERCATOR_LAT,
    MAX_SEGMENT_INTERPOLATION_M,
    MICRO_EXPLORER_ZOOM,
    VALID_EXPLORER_ZOOMS,
    compute_chronological_new_tile_counts,
    compute_max_cluster,
    compute_max_square,
    compute_max_square_expansion_advisor,
    decode_polyline,
    encode_polyline,
    haversine_distance_m,
    lat_lon_to_tile,
    latlon_to_tile_float,
    rasterize_polyline_tiles,
    rasterize_segment_dda,
    rasterize_track_segments,
    tile_bounds_wgs84,
    tile_center_wgs84,
    tile_key,
    unpack_tile_key,
)
from trip_telemetry.tiles import _find_best_windows


def test_public_constants() -> None:
    assert DEFAULT_EXPLORER_ZOOM == 14
    assert MICRO_EXPLORER_ZOOM == 17
    assert VALID_EXPLORER_ZOOMS == frozenset({14, 17})
    assert math.isclose(MAX_MERCATOR_LAT, 85.05112877980659, rel_tol=0.0, abs_tol=1e-12)
    assert MAX_SEGMENT_INTERPOLATION_M == 5_000.0


def test_polyline_google_reference_vector() -> None:
    reference_encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    expected_points = [
        (38.5, -120.2),
        (40.7, -120.95),
        (43.252, -126.453),
    ]
    decoded = decode_polyline(reference_encoded)
    assert len(decoded) == len(expected_points)
    for (lat, lon), (exp_lat, exp_lon) in zip(decoded, expected_points, strict=True):
        assert math.isclose(lat, exp_lat, abs_tol=1e-5)
        assert math.isclose(lon, exp_lon, abs_tol=1e-5)

    assert encode_polyline(expected_points) == reference_encoded


def test_polyline_empty_and_zero_points() -> None:
    assert decode_polyline("") == []
    assert encode_polyline([]) == ""

    zero_pts = [(0.0, 0.0), (0.0, 0.0), (-0.00001, 0.00001)]
    encoded = encode_polyline(zero_pts)
    decoded = decode_polyline(encoded)
    assert decoded == [(0.0, 0.0), (0.0, 0.0), (-0.00001, 0.00001)]


@pytest.mark.parametrize(
    "bad_polyline",
    [
        "_p~i",
        "_p~iF",
        "invalid space",
        "abc\x7fdef",
        "~" * 10 + "?",
        "é",
    ],
)
def test_decode_polyline_rejects_malformed(bad_polyline: str) -> None:
    with pytest.raises(ValueError):
        decode_polyline(bad_polyline)


def test_encode_polyline_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        encode_polyline([(float("nan"), 10.0)])
    with pytest.raises(ValueError):
        encode_polyline([(10.0, float("inf"))])


@given(
    st.lists(
        st.tuples(
            st.floats(min_value=-85.0, max_value=85.0, allow_nan=False, allow_infinity=False),
            st.floats(min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False),
        ),
        min_size=0,
        max_size=25,
    )
)
@settings(max_examples=75, deadline=None)
def test_polyline_roundtrip_property(points: list[tuple[float, float]]) -> None:
    encoded = encode_polyline(points)
    decoded = decode_polyline(encoded)
    assert len(decoded) == len(points)
    for (lat, lon), (dlat, dlon) in zip(points, decoded, strict=True):
        assert abs(lat - dlat) <= 1e-5 + 1e-12
        assert abs(lon - dlon) <= 1e-5 + 1e-12


def test_web_mercator_origin_clamping_and_wrapping() -> None:
    assert lat_lon_to_tile(0.0, 0.0, 0) == (0, 0)
    assert lat_lon_to_tile(0.0, 0.0, 1) == (1, 1)
    assert lat_lon_to_tile(0.0, 0.0, 14) == (8192, 8192)
    assert lat_lon_to_tile(0.0, 0.0, 17) == (65536, 65536)

    assert lat_lon_to_tile(90.0, 0.0, 14) == (8192, 0)
    assert lat_lon_to_tile(-90.0, 0.0, 14) == (8192, 16383)
    assert lat_lon_to_tile(-90.0, 0.0, 20) == (524288, 1048575)

    assert lat_lon_to_tile(0.0, -180.0, 14) == (0, 8192)
    assert lat_lon_to_tile(0.0, 180.0, 14) == (0, 8192)
    assert lat_lon_to_tile(0.0, 540.0, 14) == (0, 8192)
    assert lat_lon_to_tile(0.0, -540.0, 14) == (0, 8192)

    xf, yf = latlon_to_tile_float(-90.0, 179.999999999, 20)
    assert 0.0 <= xf < float(1 << 20)
    assert 0.0 <= yf < float(1 << 20)


def test_tile_bounds_and_center_inverse() -> None:
    for zoom in (0, 1, 14, 17, 20):
        mid = (1 << zoom) // 2
        for x, y in ((0, 0), (mid, mid), ((1 << zoom) - 1, (1 << zoom) - 1)):
            west, south, east, north = tile_bounds_wgs84(x, y, zoom)
            clat, clon = tile_center_wgs84(x, y, zoom)
            assert west < clon < east
            assert south < clat < north
            assert lat_lon_to_tile(clat, clon, zoom) == (x, y)

    w1, s1, e1, n1 = tile_bounds_wgs84(8912, 5410, 14)
    w2, s2, e2, n2 = tile_bounds_wgs84(8913, 5410, 14)
    w3, s3, e3, n3 = tile_bounds_wgs84(8912, 5411, 14)
    assert math.isclose(e1, w2, abs_tol=1e-12)
    assert math.isclose(s1, n3, abs_tol=1e-12)
    assert math.isclose(n1, n2, abs_tol=1e-12)
    assert math.isclose(w1, w3, abs_tol=1e-12)


def test_tile_key_pack_unpack_and_validation() -> None:
    for zoom, x, y in ((0, 0, 0), (14, 8912, 5410), (17, 131071, 0), (20, 1048575, 1048575)):
        packed = tile_key(zoom, x, y)
        assert packed == ((zoom << 40) | (x << 20) | y)
        assert unpack_tile_key(packed) == (zoom, x, y)

    with pytest.raises(ValueError):
        lat_lon_to_tile(0.0, 0.0, -1)
    with pytest.raises(ValueError):
        lat_lon_to_tile(0.0, 0.0, 21)
    with pytest.raises(ValueError):
        lat_lon_to_tile(float("nan"), 0.0, 14)
    with pytest.raises(ValueError):
        tile_key(14, 16384, 0)
    with pytest.raises(ValueError):
        unpack_tile_key(-1)
    with pytest.raises(ValueError):
        unpack_tile_key((21 << 40) | 1)
    with pytest.raises(ValueError):
        unpack_tile_key((14 << 40) | (16384 << 20))


def test_dda_exact_diagonal_corner_traversal() -> None:
    # Center of (8192, 8192) to center of (8193, 8193) crosses the exact corner (8193.0, 8193.0).
    lat0, lon0 = tile_center_wgs84(8192, 8192, 14)
    lat1, lon1 = tile_center_wgs84(8193, 8193, 14)
    out: set[tuple[int, int]] = set()
    rasterize_segment_dda(lat0, lon0, lat1, lon1, 14, out)
    assert out == {(8192, 8192), (8193, 8193)}

    lat2, lon2 = tile_center_wgs84(8192, 8193, 14)
    lat3, lon3 = tile_center_wgs84(8193, 8192, 14)
    out_anti: set[tuple[int, int]] = set()
    rasterize_segment_dda(lat2, lon2, lat3, lon3, 14, out_anti)
    assert out_anti == {(8192, 8193), (8193, 8192)}


def test_dda_5km_gps_jump_guard() -> None:
    lat0, lon0 = 52.0, 21.0
    lat1, lon1 = 52.0, 21.065
    assert haversine_distance_m(lat0, lon0, lat1, lon1) < MAX_SEGMENT_INTERPOLATION_M
    near_tiles: set[tuple[int, int]] = set()
    rasterize_segment_dda(lat0, lon0, lat1, lon1, 14, near_tiles)
    assert len(near_tiles) >= 3

    # ~5.5 km segment -> exceeds MAX_SEGMENT_INTERPOLATION_M, emits ONLY start and end tiles.
    lat2, lon2 = 52.0, 21.08
    assert haversine_distance_m(lat0, lon0, lat2, lon2) > MAX_SEGMENT_INTERPOLATION_M
    jump_tiles: set[tuple[int, int]] = set()
    rasterize_segment_dda(lat0, lon0, lat2, lon2, 14, jump_tiles)
    assert jump_tiles == {lat_lon_to_tile(lat0, lon0, 14), lat_lon_to_tile(lat2, lon2, 14)}


def test_dda_antimeridian_split_and_symmetry() -> None:
    lat0, lon0 = -16.8, 179.99
    lat1, lon1 = -16.802, -179.99
    assert haversine_distance_m(lat0, lon0, lat1, lon1) < MAX_SEGMENT_INTERPOLATION_M

    fwd: set[tuple[int, int]] = set()
    rev: set[tuple[int, int]] = set()
    rasterize_segment_dda(lat0, lon0, lat1, lon1, 14, fwd)
    rasterize_segment_dda(lat1, lon1, lat0, lon0, 14, rev)

    assert fwd == rev
    xs = {x for x, _ in fwd}
    assert 0 in xs
    assert 16383 in xs
    # Must NOT traverse interior globe columns
    assert all(x <= 2 or x >= 16381 for x in xs)


def test_rasterize_polyline_and_track_segments() -> None:
    pts = [
        tile_center_wgs84(8912, 5410, 14),
        tile_center_wgs84(8914, 5410, 14),
    ]
    encoded = encode_polyline(pts)
    from_pts = rasterize_polyline_tiles(pts, 14)
    from_str = rasterize_polyline_tiles(encoded, 14)
    assert from_pts == {(8912, 5410), (8913, 5410), (8914, 5410)}
    assert from_str == from_pts

    assert rasterize_polyline_tiles([], 14) == set()
    assert rasterize_polyline_tiles("", 14) == set()
    assert rasterize_polyline_tiles([pts[0]], 14) == {(8912, 5410)}

    # Multi-segment track: gap between (8912, 5410) and (8914, 5410) is < 5 km,
    # yet separate segments must NOT interpolate tile (8913, 5410).
    seg_tiles = rasterize_track_segments([[pts[0]], [pts[1]]], 14)
    assert seg_tiles == {(8912, 5410), (8914, 5410)}


def test_max_square_empty_single_and_ties() -> None:
    empty = compute_max_square([])
    assert empty.size == 0
    assert empty.anchors == ()
    assert empty.tiles == frozenset()

    single = compute_max_square([(10, 20)])
    assert single.size == 1
    assert single.anchors == ((10, 20),)
    assert single.tiles == frozenset({(10, 20)})

    rect = {(x, y) for x in range(5, 9) for y in range(5, 8)}
    res = compute_max_square(rect)
    assert res.size == 3
    assert res.anchors == ((5, 5), (6, 5))
    assert res.tiles == frozenset(rect)

    disjoint = {
        (0, 0),
        (1, 0),
        (0, 1),
        (1, 1),
        (50, 50),
        (51, 50),
        (50, 51),
        (51, 51),
        (99, 99),
    }
    res2 = compute_max_square(disjoint)
    assert res2.size == 2
    assert res2.anchors == ((0, 0), (50, 50))
    assert (99, 99) not in res2.tiles
    assert len(res2.tiles) == 8


def test_max_cluster_basic_and_deep_chain_no_recursion_error() -> None:
    assert compute_max_cluster([]).size == 0

    cross = {(5, 5), (4, 5), (6, 5), (5, 4), (5, 6)}
    res = compute_max_cluster(cross)
    assert res.cluster_tiles == frozenset({(5, 5)})
    assert res.max_cluster_tiles == frozenset({(5, 5)})
    assert res.size == 1

    # Deep 2,500-tile cluster chain (3 rows x 2,502 cols -> 2,500 interior core tiles)
    # Verifies iterative BFS never triggers Python's RecursionError (~1000 depth limit).
    length = 2_500
    deep_strip = {(x, y) for x in range(length + 2) for y in range(3)}
    deep_res = compute_max_cluster(deep_strip)
    assert deep_res.size == length
    assert len(deep_res.cluster_tiles) == length
    assert deep_res.max_cluster_tiles == frozenset((x, 1) for x in range(1, length + 1))


def test_expansion_advisor_cardinal_shifts_and_global_obstacle_escape() -> None:
    empty = compute_max_square_expansion_advisor([], zoom=14)
    assert empty.target_size == 1
    assert empty.candidates == ()
    assert empty.next_tier_target_size == 2
    assert empty.next_tier_best_missing_count == 4

    full_z0 = compute_max_square_expansion_advisor([(0, 0)], zoom=0)
    assert full_z0.target_size == 2
    assert full_z0.candidates == ()
    assert full_z0.next_tier_target_size == 3
    assert full_z0.next_tier_best_missing_count == 0

    visited = {(x, y) for x in range(100, 103) for y in range(100, 103)}
    ne_border = {(x, 99) for x in range(100, 104)} | {(103, y) for y in range(100, 103)}
    gap_tiles = {(101, 99), (103, 101)}
    visited |= ne_border - gap_tiles

    assert compute_max_square(visited).anchors == ((100, 100),)
    advisor = compute_max_square_expansion_advisor(visited, zoom=14, max_candidates=5)
    assert advisor.target_size == 4
    assert advisor.next_tier_target_size == 5
    assert len(advisor.candidates) >= 1
    top = advisor.candidates[0]
    assert top.direction == "NE"
    assert top.id == "ne-100-99"
    assert (top.anchor_x, top.anchor_y) == (100, 99)
    assert top.missing_count == 2
    assert top.overlaps_current_max_square is True
    assert {(t.x, t.y) for t in top.missing_tiles} == gap_tiles
    for mt in top.missing_tiles:
        exp_lat, exp_lon = tile_center_wgs84(mt.x, mt.y, 14)
        assert math.isclose(mt.center_lat, exp_lat, abs_tol=1e-12)
        assert math.isclose(mt.center_lon, exp_lon, abs_tol=1e-12)

    # Global obstacle-blocked scenario:
    # Current 3x3 max square at (100, 100) has no surrounding tiles (all 4 local shifts need 7).
    # Far away at (200, 200), a 4x4 window has 15/16 tiles visited (missing only (201, 201), so its
    # own max square is only 2x2!), needing only 1 missing tile to jump straight to 4x4.
    blocked_visited = {(x, y) for x in range(100, 103) for y in range(100, 103)}
    near_4x4 = {(x, y) for x in range(200, 204) for y in range(200, 204)} - {(201, 201)}
    blocked_visited |= near_4x4
    assert compute_max_square(blocked_visited).size == 3

    global_adv = compute_max_square_expansion_advisor(blocked_visited, zoom=14, max_candidates=5)
    assert global_adv.target_size == 4
    best = global_adv.candidates[0]
    assert (best.anchor_x, best.anchor_y) == (200, 200)
    assert best.direction == "GLOBAL"
    assert best.missing_count == 1
    assert best.overlaps_current_max_square is False
    assert [(t.x, t.y) for t in best.missing_tiles] == [(201, 201)]


def test_expansion_advisor_clamps_to_world_bounds_and_sparse_fallback() -> None:
    res = compute_max_square_expansion_advisor([(0, 0)], zoom=2, max_candidates=5)
    assert res.target_size == 2
    for cand in res.candidates:
        assert 0 <= cand.anchor_x <= (1 << 2) - res.target_size
        assert 0 <= cand.anchor_y <= (1 << 2) - res.target_size

    # Verify sparse vote fallback agrees with dense solver when max_dense_grid_cells is tiny
    visited = {(x, y) for x in range(10, 14) for y in range(10, 14)} - {(11, 11)}
    max_sq = compute_max_square(visited)
    dense = _find_best_windows(
        visited,
        4,
        1 << 14,
        5,
        current_size=max_sq.size,
        current_anchors=max_sq.anchors,
        local_directions={},
        dp={},
        max_dense_grid_cells=10_000,
    )
    sparse = _find_best_windows(
        visited,
        4,
        1 << 14,
        5,
        current_size=max_sq.size,
        current_anchors=max_sq.anchors,
        local_directions={},
        dp={},
        max_dense_grid_cells=1,
    )
    assert dense[0][0] == sparse[0][0] == 1
    assert (dense[0][1], dense[0][2]) == (sparse[0][1], sparse[0][2]) == (10, 10)


def test_chronological_new_tile_counts() -> None:
    assert compute_chronological_new_tile_counts([]) == {}

    k1 = tile_key(14, 100, 200)
    k2 = tile_key(14, 101, 200)
    k3 = tile_key(14, 102, 200)
    ordered = [
        ("act-c", [k1, k2, k1]),
        ("act-a", [k2, k3]),
        ("act-b", [k1, k3]),
    ]
    counts = compute_chronological_new_tile_counts(ordered)
    assert list(counts.keys()) == ["act-c", "act-a", "act-b"]
    assert counts == {"act-c": 2, "act-a": 1, "act-b": 0}

    tuple_counts = compute_chronological_new_tile_counts(
        [
            (1, [(10, 10), (10, 11)]),
            (2, [(10, 11), (10, 12)]),
        ]
    )
    assert tuple_counts == {1: 2, 2: 1}


def _brute_force_max_square(
    visited: set[tuple[int, int]],
    width: int,
    height: int,
) -> tuple[int, tuple[tuple[int, int], ...]]:
    best_size = 0
    best_anchors: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            max_possible = min(width - x, height - y)
            for size in range(1, max_possible + 1):
                if all((x + dx, y + dy) in visited for dy in range(size) for dx in range(size)):
                    if size > best_size:
                        best_size = size
                        best_anchors = [(x, y)]
                    elif size == best_size:
                        best_anchors.append((x, y))
                else:
                    break
    return best_size, tuple(sorted(best_anchors))


def _brute_force_max_cluster(visited: set[tuple[int, int]]) -> tuple[set[tuple[int, int]], int]:
    cores = {
        (x, y)
        for x, y in visited
        if {(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)}.issubset(visited)
    }
    seen: set[tuple[int, int]] = set()
    max_len = 0
    for node in cores:
        if node in seen:
            continue
        stack = [node]
        seen.add(node)
        count = 0
        while stack:
            cx, cy = stack.pop()
            count += 1
            for nb in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if nb in cores and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        max_len = max(max_len, count)
    return cores, max_len


def _brute_force_min_missing(
    visited: set[tuple[int, int]],
    grid_size: int,
    window_size: int,
) -> int:
    if window_size > grid_size:
        return 0
    best = window_size * window_size
    for ay in range(grid_size - window_size + 1):
        for ax in range(grid_size - window_size + 1):
            missing = sum(
                1
                for dy in range(window_size)
                for dx in range(window_size)
                if (ax + dx, ay + dy) not in visited
            )
            if missing < best:
                best = missing
    return best


@st.composite
def _small_grid_tiles(draw: st.DrawFn, side: int = 12) -> set[tuple[int, int]]:
    coords: set[tuple[int, int]] = draw(
        st.sets(
            st.tuples(st.integers(0, side - 1), st.integers(0, side - 1)),
            min_size=0,
            max_size=side * side,
        )
    )
    return coords


@given(_small_grid_tiles(side=12))
@settings(max_examples=80, deadline=None)
def test_max_square_matches_brute_force_oracle(visited: set[tuple[int, int]]) -> None:
    expected_size, expected_anchors = _brute_force_max_square(visited, 12, 12)
    result = compute_max_square(visited)
    assert result.size == expected_size
    assert result.anchors == expected_anchors
    if expected_size == 0:
        assert result.tiles == frozenset()
    else:
        for ax, ay in result.anchors:
            for dy in range(expected_size):
                for dx in range(expected_size):
                    assert (ax + dx, ay + dy) in result.tiles


@given(_small_grid_tiles(side=12))
@settings(max_examples=80, deadline=None)
def test_max_cluster_matches_naive_oracle(visited: set[tuple[int, int]]) -> None:
    expected_cores, expected_max = _brute_force_max_cluster(visited)
    result = compute_max_cluster(visited)
    assert result.cluster_tiles == frozenset(expected_cores)
    assert result.size == expected_max
    assert len(result.max_cluster_tiles) == expected_max
    assert result.max_cluster_tiles.issubset(result.cluster_tiles)


@given(
    st.sets(
        st.tuples(st.integers(0, 11), st.integers(0, 11)),
        min_size=1,
        max_size=90,
    )
)
@settings(max_examples=70, deadline=None)
def test_expansion_advisor_matches_brute_force_oracle(visited: set[tuple[int, int]]) -> None:
    # Zoom 4 gives a 16x16 world grid; visited tiles sit in [0, 11]^2.
    zoom = 4
    grid_size = 1 << zoom
    advisor = compute_max_square_expansion_advisor(visited, zoom=zoom, max_candidates=5)

    current_size = compute_max_square(visited).size
    assert advisor.target_size == current_size + 1
    assert advisor.next_tier_target_size == current_size + 2

    oracle_best = _brute_force_min_missing(visited, grid_size, advisor.target_size)
    assert len(advisor.candidates) >= 1
    assert advisor.candidates[0].missing_count == oracle_best

    for cand in advisor.candidates:
        assert 0 <= cand.anchor_x <= grid_size - cand.size
        assert 0 <= cand.anchor_y <= grid_size - cand.size
        actual_missing = {
            (cand.anchor_x + dx, cand.anchor_y + dy)
            for dy in range(cand.size)
            for dx in range(cand.size)
            if (cand.anchor_x + dx, cand.anchor_y + dy) not in visited
        }
        assert cand.missing_count == len(actual_missing)
        assert {(t.x, t.y) for t in cand.missing_tiles} == actual_missing

    oracle_next_tier = _brute_force_min_missing(visited, grid_size, advisor.next_tier_target_size)
    assert advisor.next_tier_best_missing_count == oracle_next_tier


@given(
    lat0=st.floats(min_value=-70.0, max_value=70.0, allow_nan=False, allow_infinity=False),
    lon0=st.floats(min_value=-175.0, max_value=175.0, allow_nan=False, allow_infinity=False),
    dlat=st.floats(min_value=-0.025, max_value=0.025, allow_nan=False, allow_infinity=False),
    dlon=st.floats(min_value=-0.025, max_value=0.025, allow_nan=False, allow_infinity=False),
    zoom=st.sampled_from([14, 17]),
)
@settings(max_examples=100, deadline=None)
def test_dda_continuity_symmetry_and_supercover_property(
    lat0: float,
    lon0: float,
    dlat: float,
    dlon: float,
    zoom: int,
) -> None:
    lat1 = lat0 + dlat
    lon1 = lon0 + dlon
    if haversine_distance_m(lat0, lon0, lat1, lon1) > MAX_SEGMENT_INTERPOLATION_M:
        return

    fwd: set[tuple[int, int]] = set()
    rev: set[tuple[int, int]] = set()
    rasterize_segment_dda(lat0, lon0, lat1, lon1, zoom, fwd)
    rasterize_segment_dda(lat1, lon1, lat0, lon0, zoom, rev)

    assert fwd == rev

    t0 = lat_lon_to_tile(lat0, lon0, zoom)
    t1 = lat_lon_to_tile(lat1, lon1, zoom)
    assert t0 in fwd
    assert t1 in fwd

    min_x, max_x = min(t0[0], t1[0]), max(t0[0], t1[0])
    min_y, max_y = min(t0[1], t1[1]), max(t0[1], t1[1])
    for tx, ty in fwd:
        assert min_x <= tx <= max_x
        assert min_y <= ty <= max_y

    visited_bfs: set[tuple[int, int]] = {t0}
    queue: deque[tuple[int, int]] = deque((t0,))
    while queue:
        cx, cy = queue.popleft()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nb = (cx + dx, cy + dy)
                if nb in fwd and nb not in visited_bfs:
                    visited_bfs.add(nb)
                    queue.append(nb)
    assert visited_bfs == fwd

    x0, y0 = latlon_to_tile_float(lat0, lon0, zoom)
    x1, y1 = latlon_to_tile_float(lat1, lon1, zoom)
    samples = 64
    for step in range(1, samples):
        frac = step / samples
        xs = x0 + frac * (x1 - x0)
        ys = y0 + frac * (y1 - y0)
        # Skip samples sitting directly on integer grid lines where float rounding is ambiguous
        near_int_x = abs(xs - round(xs)) < 1e-8
        near_int_y = abs(ys - round(ys)) < 1e-8
        if near_int_x or near_int_y:
            continue
        assert (int(math.floor(xs)), int(math.floor(ys))) in fwd
