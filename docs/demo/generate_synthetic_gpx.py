"""Generate a fictional route for local demos, without personal activity data.

Usage: python docs/demo/generate_synthetic_gpx.py /path/to/demo.gpx
The coordinates follow mathematical curves, not roads or a recorded activity.
"""

from __future__ import annotations

import argparse
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET


def generate_route() -> bytes:
    namespace = "http://www.topografix.com/GPX/1/1"
    ET.register_namespace("", namespace)

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    root = ET.Element(tag("gpx"), version="1.1", creator="Trip Animator synthetic demo")
    track = ET.SubElement(root, tag("trk"))
    ET.SubElement(track, tag("name")).text = "Synthetic Alpine Ride"
    segment = ET.SubElement(track, tag("trkseg"))
    start = datetime(2020, 1, 1, 12, tzinfo=UTC)
    for index in range(361):
        progress = index / 360
        latitude = 46.58 + 0.045 * progress + 0.003 * math.sin(6 * math.pi * progress)
        longitude = 7.96 + 0.075 * progress + 0.008 * math.sin(4 * math.pi * progress)
        elevation = 1100 + 650 * progress + 90 * math.sin(4 * math.pi * progress)
        point = ET.SubElement(
            segment, tag("trkpt"), lat=f"{latitude:.7f}", lon=f"{longitude:.7f}"
        )
        ET.SubElement(point, tag("ele")).text = f"{elevation:.2f}"
        ET.SubElement(point, tag("time")).text = (
            start + timedelta(seconds=5 * index)
        ).isoformat().replace("+00:00", "Z")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    arguments.output.write_bytes(generate_route())
    print(f"Generated 361 synthetic GPX points: {arguments.output}")
