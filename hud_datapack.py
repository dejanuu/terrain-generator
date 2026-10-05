"""
Writes a small Minecraft data pack into the built world that shows a bar
at the top of the screen with where you are in real life:

    Howard County, MD  ·  near Ellicott City  ·  39.2673° N  76.7983° W

No mods needed -- data packs are built into Minecraft Java. It works in
singleplayer (in multiplayer everyone sees the same bar, for whichever
player was checked last).

How it works in-game: twice a second it reads the player's block X/Z,
converts them to latitude/longitude with integer math on scoreboards
(a quadratic fit computed here, in Python, for this one build),
finds the nearest town by squared distance, and updates a boss bar.
"""

import json
import math
import os
import shutil

import numpy as np

NS = "county_hud"        # data pack namespace
OBJ = "chud"             # scoreboard objective
BAR = f"{NS}:info"       # boss bar id
UPDATE_TICKS = 10        # update twice per second
MAX_TOWNS = 200          # keeps the per-update command count modest

PACK_DIR_NAME = "county_location_hud"


# --------------------------------------------------------------------------
# Geometry: block coordinates <-> lat/lon
# --------------------------------------------------------------------------

def _features(bx, bz, K):
    u, v = bx / K, bz / K
    return np.column_stack([np.ones_like(bx), bx, bz, u * u, v * v, u * v])


def fit_block_to_latlon(transform, crs, width, height, origin_x, origin_z, K):
    """
    Least-squares fit of lat (and lon) as a quadratic in block x/z:
        lat = a0 + ax*x + az*z + auu*u^2 + avv*v^2 + auv*u*v,   u = x/K, v = z/K
    The squared terms soak up the curvature of the map projection, so the
    error stays small even for huge western counties.
    Returns (lat_coeffs, lon_coeffs, max_error_m).
    """
    from rasterio.warp import transform as warp_transform

    gx = np.linspace(0, width - 1, 41)
    gz = np.linspace(0, height - 1, 41)
    GX, GZ = np.meshgrid(gx, gz)
    GX, GZ = GX.ravel(), GZ.ravel()
    # center of each block column, in projected meters
    xs, ys = transform * (GX + 0.5, GZ + 0.5)
    lons, lats = warp_transform(crs, "EPSG:4326", list(xs), list(ys))
    lons, lats = np.array(lons), np.array(lats)

    A = _features(GX + origin_x, GZ + origin_z, K)
    lat_c, *_ = np.linalg.lstsq(A, lats, rcond=None)
    lon_c, *_ = np.linalg.lstsq(A, lons, rcond=None)

    err_lat = np.abs(A @ lat_c - lats) * 111_320
    err_lon = np.abs(A @ lon_c - lons) * 111_320 * np.cos(np.radians(lats))
    return lat_c, lon_c, float(max(err_lat.max(), err_lon.max()))


def latlon_to_block(lats, lons, transform, crs, origin_x, origin_z):
    """Real lat/lon -> fractional (block x, block z)."""
    from rasterio.warp import transform as warp_transform

    xs, ys = warp_transform("EPSG:4326", crs, list(lons), list(lats))
    cols, rows = ~transform * (np.array(xs), np.array(ys))
    return np.array(cols) + origin_x, np.array(rows) + origin_z


# --------------------------------------------------------------------------
# Integer math plan (scoreboards are 32-bit ints)
# --------------------------------------------------------------------------

UNITS = 100_000  # lat/lon handled in units of 1e-5 degrees (~1 m)
INT_LIMIT = 1_500_000_000  # stay well inside 2^31


def _pick_divisor(weighted_max):
    D = 1
    while D < 10**7 and weighted_max * D * 10 < INT_LIMIT:
        D *= 10
    return D


def plan_integer_math(lat_c, lon_c, x_range, z_range, K):
    """
    value = C0 + (Cx*x + Cz*z) / D1 + (Cuu*u*u + Cvv*v*v + Cuv*u*v) / D2
    with u = x/K, v = z/K (integer division), in 1e-5 degree units. D1 and
    D2 are the largest powers of 10 that can't overflow 32-bit scoreboards
    anywhere in the clamped x/z range.
    """
    xmax = max(abs(x_range[0]), abs(x_range[1]))
    zmax = max(abs(z_range[0]), abs(z_range[1]))
    umax, vmax = xmax // K + 1, zmax // K + 1
    plans = {}
    for key, c in (("lat", lat_c), ("lon", lon_c)):
        c = np.asarray(c) * UNITS
        D1 = _pick_divisor(abs(c[1]) * xmax + abs(c[2]) * zmax)
        D2 = _pick_divisor(abs(c[3]) * umax**2 + abs(c[4]) * vmax**2 + abs(c[5]) * umax * vmax)
        plans[key] = dict(C0=int(round(c[0])), Cx=int(round(c[1] * D1)), Cz=int(round(c[2] * D1)), D1=D1,
                          Cuu=int(round(c[3] * D2)), Cvv=int(round(c[4] * D2)), Cuv=int(round(c[5] * D2)), D2=D2)
    return plans


# --------------------------------------------------------------------------
# Writing the data pack
# --------------------------------------------------------------------------

def _text(s, color=None, bold=False):
    d = {"text": s}
    if color:
        d["color"] = color
    if bold:
        d["bold"] = True
    return d


def _score(name, color=None):
    d = {"score": {"name": name, "objective": OBJ}}
    if color:
        d["color"] = color
    return d


def _bar_name(county_label, town, lat_letter, lon_letter):
    """Boss bar title as a text component (works as JSON and as 1.21.5+ SNBT)."""
    num = "white"
    extra = [_text(county_label, "gold", bold=True)]
    if town:
        extra += [_text("  ·  near ", "gray"), _text(town, "aqua")]
    extra += [
        _text("  ·  ", "gray"),
        _score("#lat_deg", num), _text(".", num),
        _score("#lat_d1", num), _score("#lat_d2", num), _score("#lat_d3", num), _score("#lat_d4", num),
        _text(f"° {lat_letter}  ", num),
        _score("#lon_deg", num), _text(".", num),
        _score("#lon_d1", num), _score("#lon_d2", num), _score("#lon_d3", num), _score("#lon_d4", num),
        _text(f"° {lon_letter}", num),
    ]
    # Every list element is a compound, so it's valid in both formats.
    return json.dumps({"text": "", "extra": extra}, ensure_ascii=False)


def _value_lines(key, plan):
    """Commands computing #<key> (lat or lon, 1e-5 deg units) from #x/#z."""
    o = OBJ
    return [
        f"scoreboard players operation #{key} {o} = #x {o}",
        f"scoreboard players operation #{key} {o} *= #{key}_cx {o}",
        f"scoreboard players operation #tmp {o} = #z {o}",
        f"scoreboard players operation #tmp {o} *= #{key}_cz {o}",
        f"scoreboard players operation #{key} {o} += #tmp {o}",
        f"scoreboard players operation #{key} {o} /= #{key}_D1 {o}",
        f"scoreboard players operation #q {o} = #uu {o}",
        f"scoreboard players operation #q {o} *= #{key}_cuu {o}",
        f"scoreboard players operation #tmp {o} = #vv {o}",
        f"scoreboard players operation #tmp {o} *= #{key}_cvv {o}",
        f"scoreboard players operation #q {o} += #tmp {o}",
        f"scoreboard players operation #tmp {o} = #uv {o}",
        f"scoreboard players operation #tmp {o} *= #{key}_cuv {o}",
        f"scoreboard players operation #q {o} += #tmp {o}",
        f"scoreboard players operation #q {o} /= #{key}_D2 {o}",
        f"scoreboard players operation #{key} {o} += #q {o}",
        f"scoreboard players operation #{key} {o} += #{key}_c0 {o}",
        # absolute value (hemisphere letter is fixed for the build)
        f"execute if score #{key} {o} matches ..-1 run scoreboard players operation #{key} {o} *= #neg1 {o}",
        # whole degrees + 4 decimal digits
        f"scoreboard players operation #{key}_deg {o} = #{key} {o}",
        f"scoreboard players operation #{key}_deg {o} /= #c100000 {o}",
        f"scoreboard players operation #f {o} = #{key} {o}",
        f"scoreboard players operation #f {o} %= #c100000 {o}",
        f"scoreboard players operation #f {o} /= #c10 {o}",
        f"scoreboard players operation #{key}_d1 {o} = #f {o}",
        f"scoreboard players operation #{key}_d1 {o} /= #c1000 {o}",
        f"scoreboard players operation #{key}_d2 {o} = #f {o}",
        f"scoreboard players operation #{key}_d2 {o} /= #c100 {o}",
        f"scoreboard players operation #{key}_d2 {o} %= #c10 {o}",
        f"scoreboard players operation #{key}_d3 {o} = #f {o}",
        f"scoreboard players operation #{key}_d3 {o} /= #c10 {o}",
        f"scoreboard players operation #{key}_d3 {o} %= #c10 {o}",
        f"scoreboard players operation #{key}_d4 {o} = #f {o}",
        f"scoreboard players operation #{key}_d4 {o} %= #c10 {o}",
    ]


def build_functions(county_label, plans, towns, x_range, z_range, dist_div, lat_letter, lon_letter, K,
                    arrival=None):
    """Return {function_name: [lines]} for the data pack."""
    o = OBJ
    f = {}

    consts = {
        "#neg1": -1, "#c10": 10, "#c100": 100, "#c1000": 1000, "#c100000": 100000,
        "#xmin": x_range[0], "#xmax": x_range[1], "#zmin": z_range[0], "#zmax": z_range[1],
        "#ddiv": dist_div,
    }
    consts["#K"] = K
    for key, p in plans.items():
        consts.update({f"#{key}_c0": p["C0"], f"#{key}_cx": p["Cx"], f"#{key}_cz": p["Cz"],
                       f"#{key}_D1": p["D1"], f"#{key}_cuu": p["Cuu"], f"#{key}_cvv": p["Cvv"],
                       f"#{key}_cuv": p["Cuv"], f"#{key}_D2": p["D2"]})

    f["load"] = (
        [f"scoreboard objectives add {o} dummy"]
        + [f"scoreboard players set {k} {o} {v}" for k, v in consts.items()]
        + [
            f'bossbar add {BAR} {json.dumps({"text": county_label})}',
            f"bossbar set {BAR} color green",
            f"bossbar set {BAR} style progress",
            f"bossbar set {BAR} max 1",
            f"bossbar set {BAR} value 1",
            f"bossbar set {BAR} visible true",
        ]
    )

    f["tick"] = []
    if arrival:
        # The first time each player joins: put them on the ground in the
        # middle of the county (an empty void world spawns them at 0,0,
        # often inside the terrain), and make that the world spawn so
        # respawns land there too. spreadplayers finds the ground itself.
        ax, ay, az = arrival
        f["tick"].append(f"execute as @a[tag=!{NS}_arrived] run function {NS}:arrive")
        f["arrive"] = [
            f"setworldspawn {ax} {ay} {az}",
            f"spreadplayers {ax} {az} 0 1 false @s",
            f"tag @s add {NS}_arrived",
        ]
    f["tick"] += [
        f"scoreboard players add #timer {o} 1",
        f"execute if score #timer {o} matches {UPDATE_TICKS}.. run function {NS}:update",
    ]

    f["update"] = [
        f"scoreboard players set #timer {o} 0",
        f"bossbar set {BAR} players @a",
        f"execute as @a run function {NS}:player",
    ]

    player = [
        f"execute store result score #x {o} run data get entity @s Pos[0]",
        f"execute store result score #z {o} run data get entity @s Pos[2]",
        # clamp to the area the math was planned for (prevents overflow far away)
        f"execute if score #x {o} < #xmin {o} run scoreboard players operation #x {o} = #xmin {o}",
        f"execute if score #x {o} > #xmax {o} run scoreboard players operation #x {o} = #xmax {o}",
        f"execute if score #z {o} < #zmin {o} run scoreboard players operation #z {o} = #zmin {o}",
        f"execute if score #z {o} > #zmax {o} run scoreboard players operation #z {o} = #zmax {o}",
    ]
    # shared quadratic terms: u = x/K, v = z/K
    player += [
        f"scoreboard players operation #u {o} = #x {o}",
        f"scoreboard players operation #u {o} /= #K {o}",
        f"scoreboard players operation #v {o} = #z {o}",
        f"scoreboard players operation #v {o} /= #K {o}",
        f"scoreboard players operation #uu {o} = #u {o}",
        f"scoreboard players operation #uu {o} *= #u {o}",
        f"scoreboard players operation #vv {o} = #v {o}",
        f"scoreboard players operation #vv {o} *= #v {o}",
        f"scoreboard players operation #uv {o} = #u {o}",
        f"scoreboard players operation #uv {o} *= #v {o}",
    ]
    player += _value_lines("lat", plans["lat"])
    player += _value_lines("lon", plans["lon"])

    if towns:
        player += [f"scoreboard players set #best {o} 2147483647",
                   f"scoreboard players set #id {o} 0"]
        for i, (name, tx, tz) in enumerate(towns):
            player += [
                f"scoreboard players operation #dx {o} = #x {o}",
                f"scoreboard players remove #dx {o} {tx}",
                f"scoreboard players operation #dx {o} /= #ddiv {o}",
                f"scoreboard players operation #dx {o} *= #dx {o}",
                f"scoreboard players operation #dz {o} = #z {o}",
                f"scoreboard players remove #dz {o} {tz}",
                f"scoreboard players operation #dz {o} /= #ddiv {o}",
                f"scoreboard players operation #dz {o} *= #dz {o}",
                f"scoreboard players operation #dx {o} += #dz {o}",
                f"execute if score #dx {o} < #best {o} run function {NS}:pick/{i}",
            ]
            f[f"pick/{i}"] = [
                f"scoreboard players operation #best {o} = #dx {o}",
                f"scoreboard players set #id {o} {i}",
            ]
        for i, (name, tx, tz) in enumerate(towns):
            player.append(f"execute if score #id {o} matches {i} run bossbar set {BAR} name "
                          + _bar_name(county_label, name, lat_letter, lon_letter))
    else:
        player.append(f"bossbar set {BAR} name " + _bar_name(county_label, None, lat_letter, lon_letter))

    f["player"] = player
    return f


def write_datapack(world_dir, county_label, transform, crs, width, height,
                   origin_x, origin_z, mask=None, places=None, arrival=None):
    """
    Write the HUD data pack into <world>/datapacks/. `places` is a list of
    (name, lat, lon, land_area) or None. Returns a short summary string.
    """
    margin = 2000
    x_range = (origin_x - margin, origin_x + width + margin)
    z_range = (origin_z - margin, origin_z + height + margin)
    extent = max(abs(v) for v in x_range + z_range)
    K = max(1, math.ceil(extent / 2000))  # keeps u, v <= ~2000 so u*u fits easily

    lat_c, lon_c, err_m = fit_block_to_latlon(transform, crs, width, height, origin_x, origin_z, K)
    plans = plan_integer_math(lat_c, lon_c, x_range, z_range, K)

    center = _features(np.array([origin_x + width / 2]), np.array([origin_z + height / 2]), K)
    center_lat, center_lon = float((center @ lat_c)[0]), float((center @ lon_c)[0])
    lat_letter = "N" if center_lat >= 0 else "S"
    lon_letter = "E" if center_lon >= 0 else "W"

    # Towns inside the build (inside the county outline if there is one)
    towns = []
    if places:
        lats = [p[1] for p in places]
        lons = [p[2] for p in places]
        bx, bz = latlon_to_block(lats, lons, transform, crs, origin_x, origin_z)
        cand = []
        for (name, _, _, area), x, z in zip(places, bx, bz):
            col, row = int(math.floor(x - origin_x)), int(math.floor(z - origin_z))
            if not (0 <= col < width and 0 <= row < height):
                continue
            if mask is not None and not mask[row, col]:
                continue
            cand.append((area, name.replace('"', "'").replace("\\", ""), int(round(x)), int(round(z))))
        cand.sort(reverse=True)  # keep the biggest places if there are too many
        towns = [(n, x, z) for _, n, x, z in cand[:MAX_TOWNS]]

    extent = max(x_range[1] - x_range[0], z_range[1] - z_range[0])
    dist_div = max(1, math.ceil(extent / 30_000))  # (extent/div)^2 * 2 < 2^31

    funcs = build_functions(county_label, plans, towns, x_range, z_range, dist_div,
                            lat_letter, lon_letter, K, arrival)

    pack = os.path.join(world_dir, "datapacks", PACK_DIR_NAME)
    if os.path.exists(pack):
        shutil.rmtree(pack)

    # Folder names changed in 1.21 ("functions" -> "function"); write both.
    for fn_dir, tag_dir in (("function", "function"), ("functions", "functions")):
        for name, lines in funcs.items():
            path = os.path.join(pack, "data", NS, fn_dir, *name.split("/")) + ".mcfunction"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
        for tag, fn in (("load", "load"), ("tick", "tick")):
            path = os.path.join(pack, "data", "minecraft", "tags", tag_dir, f"{tag}.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"values": [f"{NS}:{fn}"]}, fh)

    # pack.mcmeta that loads on 1.20 through current versions:
    #   pack_format + supported_formats for versions up to 1.21.8,
    #   min_format + max_format for 1.21.9 and later.
    meta = {
        "pack": {
            "description": f"Real-world location bar for {county_label}",
            "pack_format": 48,
            "supported_formats": {"min_inclusive": 15, "max_inclusive": 999},
            "min_format": 15,
            "max_format": 999,
        }
    }
    with open(os.path.join(pack, "pack.mcmeta"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    return (f"{len(towns)} town(s), position accurate to ~{max(err_m, 1) + 11:.0f} m")
