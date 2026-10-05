"""
Terrain -> Minecraft pipeline entry point.

Easiest way: just run it (or double-click run.bat) and answer the questions:

    python main.py

Or say what you want on the command line:

    python main.py --county "Prince George's County, MD"
    python main.py --county "Travis, TX" --world MyTerrainWorld
    python main.py --county "Orleans Parish, LA" --meters-per-block 4

A county build only fills in land inside the county line. The scale is
picked automatically so the build stays a manageable size; override it
with --meters-per-block (horizontal) and --vertical-meters-per-block.

Older modes still work:

    python main.py --bbox -71.45 41.78 -71.35 41.85 --meters-per-block 1 \\
        --world-region-folder "C:\\...\\saves\\MyTerrainWorld\\region"
    python main.py --place "Providence, RI" --world MyTerrainWorld

Your OpenTopography key and world name are remembered in settings.json
next to this file, so you only type them once. Downloads are cached per
area in work/<area>/, so re-running the same county (say, at a different
scale) doesn't download elevation again.
"""

import argparse
import json
import math
import os
import shutil
import sys
import time

import numpy as np

from terrain_config import TerrainConfig
from fetch_data import fetch_elevation, fetch_landcover, align_rasters, county_mask
from world_builder import build_world

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")

# Build-size target for automatic scale. ~40M columns takes a few minutes
# to build and a few hundred MB of RAM; raise with --target-columns.
DEFAULT_TARGET_COLUMNS = 40_000_000
NICE_SCALES = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100]
NICE_VERTICAL = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def world_root(region_folder):
    """The world's top folder (where level.dat and datapacks/ live)."""
    d = os.path.dirname(os.path.abspath(region_folder))
    parts = os.path.normpath(d).split(os.sep)
    if parts[-3:] == ["dimensions", "minecraft", "overworld"]:
        d = os.sep.join(parts[:-3])
    return d


BUILD_INFO_NAME = "county_generator.json"


def save_build_info(region_folder, label, bbox, mpb, origin, size, crs, transform):
    """Remember how this world's map was made, so goto.py can find places in it."""
    info = {
        "label": label,
        "bbox": list(bbox),
        "meters_per_block": mpb,
        "origin": list(origin),
        "size": list(size),
        "crs": str(crs),
        "transform": list(transform)[:6],
    }
    path = os.path.join(world_root(region_folder), BUILD_INFO_NAME)
    with open(path, "w") as f:
        json.dump(info, f, indent=2)


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def saves_folder():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, ".minecraft", "saves")
    return os.path.join(os.path.expanduser("~"), ".minecraft", "saves")


def level_dat_ok(path):
    """A real Minecraft level.dat is gzip-compressed NBT: it starts with 1f 8b."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def list_worlds():
    root = saves_folder()
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if os.path.exists(os.path.join(root, d, "level.dat")))


def geocode_place(name: str):
    """Look up a place name and return its bounding box via Nominatim."""
    import requests

    print(f"[geocode] looking up '{name}' via OpenStreetMap Nominatim ...")
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": name, "format": "json", "limit": 1},
        headers={"User-Agent": "terrain-to-minecraft-script/1.0"},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise RuntimeError(f"No geocoding result for '{name}'.")
    bb = results[0]["boundingbox"]  # [south, north, west, east] as strings
    south, north, west, east = map(float, bb)
    print(f"[geocode] -> bbox (west, south, east, north) = "
          f"({west}, {south}, {east}, {north})")
    return (west, south, east, north)


def area_size_m(bbox):
    west, south, east, north = bbox
    mid_lat = (south + north) / 2
    width_m = (east - west) * 111_320 * math.cos(math.radians(mid_lat))
    height_m = (north - south) * 111_320
    return width_m, height_m


def estimate_size(bbox, meters_per_block):
    width_m, height_m = area_size_m(bbox)
    width_blocks = width_m / meters_per_block
    height_blocks = height_m / meters_per_block
    total = width_blocks * height_blocks
    print(f"[estimate] area ~ {width_m/1000:.1f} km x {height_m/1000:.1f} km "
          f"-> {width_blocks:,.0f} x {height_blocks:,.0f} blocks "
          f"= {total:,.0f} columns at {meters_per_block:g} m/block")
    return total


def auto_meters_per_block(bbox, target_columns):
    width_m, height_m = area_size_m(bbox)
    for s in NICE_SCALES:
        if (width_m / s) * (height_m / s) <= target_columns:
            return s
    return NICE_SCALES[-1]


def auto_vertical(elev_inside, config):
    """
    Pick (reference_sea_level_m, vertical_meters_per_block) so the area's
    whole elevation range fits under the build limit.
      - Low-lying areas keep real sea level at Minecraft's y=64.
      - Higher inland areas put their lowest point at y=64 instead, so the
        height budget isn't wasted on hundreds of meters of nothing.
      - Vertical scale stays 1 m/block unless the relief is too tall to fit.
    """
    lo = float(np.percentile(elev_inside, 0.1))
    hi = float(elev_inside.max())
    ref = 0.0 if lo < 50 else math.floor((lo - 5) / 10) * 10
    room = (config.max_build_y - config.ceiling_margin) - config.sea_level_y - 8  # 8 for trees
    needed = (hi - ref) / room
    vert = next((v for v in NICE_VERTICAL if v >= needed), math.ceil(needed))
    return ref, max(1, vert), lo, hi


def pick_world_region_folder(args, settings, interactive):
    """Resolve where the .mca files go. Returns the region folder path."""
    if args.world_region_folder:
        return args.world_region_folder

    how_to_create = (
        "Create it in Minecraft first (Create New World -> World Type: Superflat, "
        "Customize -> 'The Void' preset), quit back to the title screen, then run "
        "this again."
    )
    world = args.world
    while True:
        if not world and interactive:
            worlds = list_worlds()
            if worlds:
                print("\nYour Minecraft worlds: " + ", ".join(worlds))
            world = ask("Minecraft world to build into", settings.get("world") or "MyTerrainWorld")
        world = world or settings.get("world")
        if not world:
            sys.exit("Say which world to build into with --world NAME "
                     "(or --world-region-folder PATH).")

        world_dir = os.path.join(saves_folder(), world)
        level_dat = os.path.join(world_dir, "level.dat")
        if os.path.exists(level_dat):
            if level_dat_ok(level_dat):
                break
            print(f"\nThe world '{world}' has a damaged level.dat (it isn't a valid "
                  f"Minecraft save file), so Minecraft won't be able to open it.")
            if not interactive:
                sys.exit("Create a new world in Minecraft and build into that instead. " + how_to_create)
            print("Pick another world, or make a new one. " + how_to_create)
            world = None
            continue
        print(f"\nCouldn't find a Minecraft world named '{world}' in:\n  {saves_folder()}")
        if not interactive:
            sys.exit(how_to_create)
        print("Type one of the names listed, or if you haven't made the world yet: " + how_to_create)
        world = None

    settings["world"] = world
    return overworld_region_folder(world_dir)


def overworld_region_folder(world_dir):
    """
    Where this world keeps its overworld terrain.
    Minecraft 26.1+ worlds:  <world>/dimensions/minecraft/overworld/region
    Older worlds:            <world>/region
    (A 26.1+ world that hasn't been opened yet has no dimensions folder, but
    Minecraft only creates worlds by opening them, so it will have one.)
    """
    new_layout = os.path.join(world_dir, "dimensions", "minecraft", "overworld")
    if os.path.isdir(new_layout):
        print("[world] this world uses the Minecraft 26.1+ folder layout")
        return os.path.join(new_layout, "region")
    return os.path.join(world_dir, "region")


def maybe_back_up_region_folder(region_folder, assume_yes):
    """Offer to move existing region files aside so builds don't overlap."""
    if not os.path.isdir(region_folder):
        return
    existing = [f for f in os.listdir(region_folder) if f.endswith(".mca")]
    if not existing:
        return
    if assume_yes:
        print(f"[world] note: {len(existing)} existing region file(s) in this world; "
              f"overlapping ones will be replaced.")
        return
    print(f"\nThis world already has {len(existing)} region file(s) "
          f"(an earlier build, or the empty area around spawn).")
    answer = ask("Move them to a backup folder so this build starts clean? [Y/n]")
    if not answer.lower().startswith("n"):
        backup = os.path.join(os.path.dirname(region_folder),
                              time.strftime("region_backup_%Y%m%d_%H%M%S"))
        shutil.move(region_folder, backup)
        os.makedirs(region_folder, exist_ok=True)
        print(f"[world] moved old region files to {backup}")


def cached(path, meta_wanted):
    """True if `path` exists and was made with the same settings."""
    try:
        with open(path + ".json") as f:
            return os.path.exists(path) and json.load(f) == meta_wanted
    except (OSError, ValueError):
        return False


def mark_cached(path, meta):
    with open(path + ".json", "w") as f:
        json.dump(meta, f)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Recreate real terrain in Minecraft Java.")
    loc = p.add_mutually_exclusive_group()
    loc.add_argument("--county", type=str, help='US county, e.g. "Travis County, TX"')
    loc.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    loc.add_argument("--place", type=str, help="Any place name to geocode (uses its bounding box)")

    p.add_argument("--meters-per-block", default="auto",
                   help="Real meters per block horizontally, or 'auto' (default)")
    p.add_argument("--vertical-meters-per-block", default="auto",
                   help="Real meters per block of height, or 'auto' (default)")
    p.add_argument("--target-columns", type=int, default=DEFAULT_TARGET_COLUMNS,
                   help="Size target used when scale is 'auto'")
    p.add_argument("--world", type=str, help="Name of the Minecraft world (in your saves folder)")
    p.add_argument("--world-region-folder", type=str, help="Or: exact path to a world's region folder")
    p.add_argument("--work-dir", type=str, default=None,
                   help="Where downloads are cached (default: work/<area name>)")
    p.add_argument("--opentopo-api-key", type=str, default=None,
                   help="Free key from https://portal.opentopography.org "
                        "(or set OPENTOPOGRAPHY_API_KEY, or let it ask once)")
    p.add_argument("--origin-x", type=int, default=0)
    p.add_argument("--origin-z", type=int, default=0)
    p.add_argument("--no-trees", action="store_true")
    p.add_argument("--no-climate", action="store_true",
                   help="Use the same look everywhere (no desert / alpine / mangrove styles)")
    p.add_argument("--no-hud", action="store_true",
                   help="Don't add the real-world location bar data pack")
    p.add_argument("--skip-fetch", action="store_true",
                   help="Reuse elevation.tif/landcover.tif already in the work dir")
    p.add_argument("--max-columns", type=int, default=200_000_000)
    p.add_argument("--yes", action="store_true", help="Don't ask any questions")
    args = p.parse_args()

    settings = load_settings()
    interactive = not args.yes and sys.stdin.isatty()

    # ---- 1. Where? --------------------------------------------------------
    county = None
    if not (args.county or args.bbox or args.place):
        if not interactive:
            sys.exit("Give an area with --county, --place or --bbox.")
        args.county = ask('\nWhich US county? (e.g. "Prince George\'s County, MD")')

    if args.county:
        from counties import find_county
        cache_dir = os.path.join(HERE, "work")
        while True:
            try:
                county = find_county(args.county, cache_dir)
                break
            except LookupError as e:
                print(f"\n{e}")
                if not interactive:
                    sys.exit(1)
                args.county = ask("\nTry again")
        bbox = county.bbox
        area_name = county.slug
        print(f"[county] {county.label}")
    elif args.bbox:
        bbox = tuple(args.bbox)
        area_name = "bbox_" + "_".join(f"{v:.3f}" for v in bbox).replace("-", "m")
    else:
        bbox = geocode_place(args.place)
        area_name = "place_" + "".join(c if c.isalnum() else "_" for c in args.place.lower())

    # ---- 2. How big? ------------------------------------------------------
    if str(args.meters_per_block).lower() == "auto":
        mpb = auto_meters_per_block(bbox, args.target_columns)
        w_m, h_m = area_size_m(bbox)
        print(f"[scale] area is ~{w_m/1000:.0f} x {h_m/1000:.0f} km -> using 1 block = {mpb:g} m "
              f"(change with --meters-per-block)")
    else:
        mpb = float(args.meters_per_block)

    total_columns = estimate_size(bbox, mpb)
    if total_columns > args.max_columns:
        sys.exit(f"\nThis would generate {total_columns:,.0f} columns, over your "
                 f"max-columns limit of {args.max_columns:,}. Increase "
                 f"--meters-per-block, shrink the area, or raise --max-columns.")

    # ---- 3. Into which world? --------------------------------------------
    region_folder = pick_world_region_folder(args, settings, interactive)

    # ---- 4. API key ---------------------------------------------------------
    api_key = (args.opentopo_api_key or os.environ.get("OPENTOPOGRAPHY_API_KEY")
               or settings.get("opentopo_api_key"))
    if not api_key and interactive and not args.skip_fetch:
        print("\nElevation data needs a free OpenTopography API key "
              "(https://portal.opentopography.org -> My Account -> myOpenTopo Authorizations).")
        api_key = ask("Paste your API key")
    if api_key:
        settings["opentopo_api_key"] = api_key
    save_settings(settings)

    if interactive:
        answer = ask(f"\nBuild ~{total_columns:,.0f} columns into {region_folder}? [y/N]")
        if answer.strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)
        maybe_back_up_region_folder(region_folder, assume_yes=False)
    else:
        maybe_back_up_region_folder(region_folder, assume_yes=True)

    # ---- 5. Download (or reuse) data ---------------------------------------
    work_dir = args.work_dir or os.path.join(HERE, "work", area_name)
    os.makedirs(work_dir, exist_ok=True)
    elevation_tif = os.path.join(work_dir, "elevation.tif")
    landcover_tif = os.path.join(work_dir, "landcover.tif")
    lc_res = max(10.0, mpb / 2)  # no need for 10 m land cover on a 20 m/block build

    if args.skip_fetch:
        print("[main] --skip-fetch set, reusing existing GeoTIFFs in work dir")
    else:
        meta = {"bbox": [round(v, 6) for v in bbox]}
        if cached(elevation_tif, meta):
            print("[elevation] reusing earlier download")
        else:
            fetch_elevation(bbox, elevation_tif, api_key)
            mark_cached(elevation_tif, meta)
        lc_meta = dict(meta, res=lc_res)
        if cached(landcover_tif, lc_meta):
            print("[landcover] reusing earlier download")
        else:
            fetch_landcover(bbox, landcover_tif, target_res_m=lc_res)
            mark_cached(landcover_tif, lc_meta)

    # ---- 6. Align, mask, scale heights, build -------------------------------
    config = TerrainConfig(
        bbox=bbox,
        meters_per_block=mpb,
        world_region_folder=region_folder,
        work_dir=work_dir,
        elevation_tif=elevation_tif,
        landcover_tif=landcover_tif,
        origin_x=args.origin_x,
        origin_z=args.origin_z,
        place_trees=not args.no_trees,
        max_columns=args.max_columns,
    )

    elevation_grid, landcover_grid, transform, crs, w, h = align_rasters(
        elevation_tif, landcover_tif, bbox, mpb
    )

    mask = None
    if county is not None:
        mask = county_mask(county.geometry, transform, w, h, crs)
        print(f"[county] {int(mask.sum()):,} of {w * h:,} columns are inside the county line")
        if not mask.any():
            sys.exit("The county outline didn't overlap the downloaded area -- this is a bug, please report it.")

    inside = elevation_grid[mask] if mask is not None else elevation_grid.reshape(-1)
    if str(args.vertical_meters_per_block).lower() == "auto":
        ref, vert, lo, hi = auto_vertical(inside, config)
        del inside  # free the copy before the memory-hungry build step
        config.reference_sea_level_m = ref
        config.vertical_meters_per_block = vert
        print(f"[heights] elevation {lo:,.0f}-{hi:,.0f} m -> y={config.sea_level_y} is {ref:,.0f} m, "
              f"1 block = {vert:g} m of height (change with --vertical-meters-per-block)")
    else:
        config.vertical_meters_per_block = float(args.vertical_meters_per_block)

    climate = None
    if not args.no_climate:
        from climate import classify, summarize
        climate = classify(elevation_grid, landcover_grid, mask, mpb, (bbox[1] + bbox[3]) / 2)
        print(f"[climate] {summarize(climate, mask)}")

    build_world(elevation_grid, landcover_grid, config, mask=mask, climate=climate)
    del climate

    save_build_info(region_folder, label=(county.label if county else (args.place or "bbox")),
                    bbox=bbox, mpb=mpb, origin=(config.origin_x, config.origin_z),
                    size=(w, h), crs=crs, transform=transform)

    # ---- 7. Arrival point: the middle of what was built ---------------------
    if mask is not None:
        zs, xs = np.nonzero(mask)
        gz, gx = int(np.median(zs)), int(np.median(xs))
        if not mask[gz, gx]:  # odd-shaped county: snap to a real inside cell
            i = len(zs) // 2
            gz, gx = int(zs[i]), int(xs[i])
    else:
        gz, gx = h // 2, w // 2
    elev = float(elevation_grid[gz, gx])
    ground = config.sea_level_y + round((elev - config.reference_sea_level_m) / config.vertical_meters_per_block)
    ground = int(min(max(ground, config.min_build_y + config.floor_margin),
                     config.max_build_y - config.ceiling_margin))
    tx, tz = config.origin_x + gx, config.origin_z + gz

    # ---- 8. Real-world location bar + safe arrival (data pack) --------------
    if not args.no_hud:
        from hud_datapack import write_datapack
        world_dir = world_root(region_folder)
        label = county.label if county else (args.place or "Real-world location")
        places = None
        if county is not None:
            try:
                from places import load_places
                places = load_places(county.state, os.path.join(HERE, "work"))
            except Exception as e:  # town names are a bonus; don't fail the build
                print(f"[hud] couldn't get town names ({e}); the bar will show coordinates only")
        summary = write_datapack(world_dir, label, transform, crs, w, h,
                                 config.origin_x, config.origin_z, mask, places,
                                 arrival=(tx, ground + 1, tz))
        print(f"[hud] added the location bar to the world: {summary}")

    print("\nDone! Now:")
    print("  1. Fully close Minecraft if it's running, then reopen it and open the world.")
    if not args.no_hud:
        print("  2. You'll be placed on the ground in the middle of the county automatically,")
        print("     and that becomes the world spawn (where you respawn).")
        print("     The bar at the top shows the nearest real town and your real lat/long.")
        print("  3. To get back to the middle later (commands must be allowed):")
    else:
        print("  2. Jump to the middle of the build (commands must be allowed):")
    print(f"       /spreadplayers {tx} {tz} 0 1 false @s")
    print("     The map runs east along +x and south along +z from 0,0.")


if __name__ == "__main__":
    main()
