"""
Find a real place in your Minecraft build and get the command to go there.

    python goto.py "39.2781, -77.0152"
    python goto.py "Merriweather Post Pavilion, Columbia MD"
    python goto.py "Ellicott City" --world "Howard County 2"

It prints a /spreadplayers command (which lands you safely on the ground)
and, on Windows, copies it to your clipboard -- just press T in Minecraft,
then Ctrl+V and Enter. Commands must be allowed in the world.

It reads the map settings the generator saved in the world folder
(county_generator.json). For a world built before that file existed, tell
it how the world was built instead:

    python goto.py "39.2781, -77.0152" --world "Howard County 2" \\
        --county "howard county, md" --meters-per-block 6
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys

from affine import Affine

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_INFO_NAME = "county_generator.json"


def saves_folder():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, ".minecraft", "saves")
    return os.path.join(os.path.expanduser("~"), ".minecraft", "saves")


def last_world():
    try:
        with open(os.path.join(HERE, "settings.json")) as f:
            return json.load(f).get("world")
    except (OSError, ValueError):
        return None


def parse_latlon(text):
    """'39.2781, -77.0152' or '39.2781 -77.0152' -> (lat, lon), else None."""
    m = re.fullmatch(r"\s*\(?\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*\)?\s*", text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def geocode(text, bbox=None):
    """Look up an address/place name with OpenStreetMap, preferring the built area."""
    import requests

    params = {"q": text, "format": "json", "limit": 1, "countrycodes": "us"}
    if bbox:
        w, s, e, n = bbox
        params["viewbox"] = f"{w},{n},{e},{s}"  # prefer results inside the build
    resp = requests.get("https://nominatim.openstreetmap.org/search", params=params,
                        headers={"User-Agent": "terrain-to-minecraft-goto/1.0"}, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    r = results[0]
    return float(r["lat"]), float(r["lon"]), r.get("display_name", text)


def load_build(world, county=None, mpb=None):
    """Return the build's map settings (dict) from the world, or rebuild them from --county."""
    world_dir = os.path.join(saves_folder(), world)
    path = os.path.join(world_dir, BUILD_INFO_NAME)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    if county and mpb:
        from counties import find_county
        from fetch_data import grid_for_bbox
        c = find_county(county, os.path.join(HERE, "work"))
        transform, w, h, crs = grid_for_bbox(c.bbox, float(mpb))
        return {"label": c.label, "bbox": list(c.bbox), "meters_per_block": float(mpb),
                "origin": [0, 0], "size": [w, h], "crs": crs, "transform": list(transform)[:6]}
    if not os.path.isdir(world_dir):
        sys.exit(f"No Minecraft world named '{world}' in {saves_folder()}")
    sys.exit(f"'{world}' doesn't have {BUILD_INFO_NAME} (it was built with an older version).\n"
             f"Add how it was built, e.g.:  --county \"howard county, md\" --meters-per-block 6\n"
             f"(the meters per block is in the build's output: 'using 1 block = 6 m')")


def to_block(info, lat, lon):
    from rasterio.warp import transform as warp

    xs, ys = warp("EPSG:4326", info["crs"], [lon], [lat])
    col, row = ~Affine(*info["transform"]) * (xs[0], ys[0])
    ox, oz = info["origin"]
    return ox + int(math.floor(col)), oz + int(math.floor(row)), col, row


def copy_to_clipboard(text):
    if os.name != "nt":
        return False
    try:
        subprocess.run("clip", input=text, text=True, shell=True, check=True)
        return True
    except Exception:
        return False


def main():
    p = argparse.ArgumentParser(description="Get a teleport command for a real place in your build.")
    p.add_argument("place", nargs="?", help='"lat, lon" or an address / place name')
    p.add_argument("--world", help="Minecraft world name (default: the last one you built into)")
    p.add_argument("--county", help="For worlds built before goto.py: the county you built")
    p.add_argument("--meters-per-block", help="For worlds built before goto.py: the scale used")
    args = p.parse_args()

    world = args.world or last_world()
    if not world:
        sys.exit("Which world? Use --world \"World Name\".")
    info = load_build(world, args.county, args.meters_per_block)

    place = args.place or input("Where to? (\"lat, lon\" or an address): ").strip()
    ll = parse_latlon(place)
    if ll:
        lat, lon = ll
        found = f"{lat:.6f}, {lon:.6f}"
    else:
        print(f"[goto] looking up '{place}' ...")
        g = geocode(place, info.get("bbox"))
        if not g:
            sys.exit("Couldn't find that place. Try adding the town and state, or paste "
                     "coordinates from Google Maps (right-click the map, click the numbers).")
        lat, lon, found = g

    x, z, col, row = to_block(info, lat, lon)
    w, h = info["size"]
    print(f"\n{found}")
    print(f"is at block X={x}, Z={z} in '{world}' ({info['label']}, 1 block = {info['meters_per_block']:g} m)")
    if not (0 <= col < w and 0 <= row < h):
        dx = max(0, -col, col - w) * info["meters_per_block"] / 1000
        dz = max(0, -row, row - h) * info["meters_per_block"] / 1000
        print(f"WARNING: that's outside the area you built (about {math.hypot(dx, dz):.1f} km past "
              f"the edge), so there's no ground there.")

    cmd = f"/spreadplayers {x} {z} 0 1 false @s"
    print(f"\n    {cmd}\n")
    if copy_to_clipboard(cmd):
        print("Copied! In Minecraft press T, then Ctrl+V and Enter.")
    else:
        print("In Minecraft press T and type the command above.")


if __name__ == "__main__":
    main()
