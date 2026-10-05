"""
Decides, column by column, what kind of place this is, so the same land
cover type can look different in Arizona than in Maryland:

    NORMAL        the default look
    DRY           desert / dry scrubland: savanna & desert biomes, coarse
                  dirt and red sand, acacia trees, no rain
    ALPINE_ROCK   above the tree line: bare stone, stony peaks biome
    ALPINE_SNOW   high peaks: snow over stone, snowy slopes biome
    WARM_WET      wetlands in the far south (Gulf Coast, Florida):
                  mangrove swamp biome

Everything is worked out from data the generator already has (land cover,
elevation, latitude) -- no extra downloads.
"""

import math

import numpy as np

NORMAL, DRY, ALPINE_ROCK, ALPINE_SNOW, WARM_WET = 0, 1, 2, 3, 4
NAMES = {NORMAL: "normal", DRY: "dry", ALPINE_ROCK: "alpine rock",
         ALPINE_SNOW: "alpine snow", WARM_WET: "warm wetland"}

# ESA WorldCover classes
TREES, SHRUB, GRASS, CROP, URBAN, BARE, SNOW, WATER, WETLAND, MANGROVE, MOSS = \
    10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100

DRY_NEIGHBORHOOD_M = 2000   # look at land within ~2 km to decide "dry"
DRY_THRESHOLD = 0.5         # more than half scrub/bare ground nearby = dry
WARM_WET_MAX_LAT = 31.0     # roughly the Gulf Coast and south


def treeline_m(lat):
    """Rough tree line for the US: ~3,500 m in Colorado/Arizona, lower further north."""
    return max(600.0, 3500.0 - 100.0 * max(0.0, abs(lat) - 39.0))


def _box_sum(a, r):
    """Sum over a (2r+1)x(2r+1) window, via a summed-area table (edges handled)."""
    c = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    h, w = a.shape
    y0 = np.clip(np.arange(h) - r, 0, h); y1 = np.clip(np.arange(h) + r + 1, 0, h)
    x0 = np.clip(np.arange(w) - r, 0, w); x1 = np.clip(np.arange(w) + r + 1, 0, w)
    return (c[y1][:, x1] - c[y0][:, x1] - c[y1][:, x0] + c[y0][:, x0])


def classify(elevation_m, landcover, mask, meters_per_block, center_lat):
    """
    Return a uint8 grid (same shape as the inputs) of climate classes.
    `mask` is True where the build has land (or None for everywhere).
    """
    h, w = landcover.shape
    climate = np.zeros((h, w), dtype=np.uint8)
    inside = np.ones((h, w), bool) if mask is None else mask
    land = inside & ~np.isin(landcover, (WATER, 0))

    # ---- DRY: share of scrub + bare ground in the surrounding ~2 km --------
    # Work on coarse ~200 m cells so this is cheap even for huge counties.
    f = max(1, int(round(200 / meters_per_block)))
    H, W = -(-h // f), -(-w // f)
    pad = ((0, H * f - h), (0, W * f - w))
    # (bool arrays + integer sums keep memory low on big counties)
    dryish = np.pad(land & np.isin(landcover, (SHRUB, BARE)), pad)
    dry_c = dryish.reshape(H, f, W, f).sum((1, 3), dtype=np.int64).astype(np.float64)
    del dryish
    land_c = np.pad(land, pad).reshape(H, f, W, f).sum((1, 3), dtype=np.int64).astype(np.float64)
    r = max(1, int(round(DRY_NEIGHBORHOOD_M / (f * meters_per_block))))
    share = _box_sum(dry_c, r) / np.maximum(_box_sum(land_c, r), 1e-6)
    dry_coarse = share > DRY_THRESHOLD
    dry = np.repeat(np.repeat(dry_coarse, f, 0), f, 1)[:h, :w]
    climate[dry & inside] = DRY

    # ---- ALPINE: above the tree line (wins over DRY) -----------------------
    tl = treeline_m(center_lat)
    alpine_ok = land & ~np.isin(landcover, (URBAN, CROP))
    climate[alpine_ok & (elevation_m > tl)] = ALPINE_ROCK
    climate[alpine_ok & (elevation_m > tl + 500)] = ALPINE_SNOW

    # ---- WARM WETLANDS: far south only -------------------------------------
    if abs(center_lat) < WARM_WET_MAX_LAT:
        climate[inside & np.isin(landcover, (WETLAND, MANGROVE))] = WARM_WET

    return climate


def summarize(climate, mask=None):
    """'dry 62%, alpine rock 3%' style summary of the non-normal classes."""
    vals = climate[mask] if mask is not None else climate.reshape(-1)
    if vals.size == 0:
        return "normal"
    counts = np.bincount(vals, minlength=5) / vals.size
    parts = [f"{NAMES[k]} {counts[k]:.0%}" for k in (DRY, ALPINE_ROCK, ALPINE_SNOW, WARM_WET)
             if counts[k] >= 0.005]
    return ", ".join(parts) if parts else "normal everywhere"
