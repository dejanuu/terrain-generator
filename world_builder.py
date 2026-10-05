"""
Turns aligned (elevation_grid, landcover_grid) numpy arrays into real
Minecraft Java .mca region files, written straight into an existing
world save's `region` folder.

Strategy:
  - Compute a Minecraft Y for every (x, z) column from elevation.
  - Walk the area one 512x512-block REGION at a time (the unit that's
    saved to disk). For each region, build the whole block volume at once
    as a numpy array of block IDs -- no per-block Python calls -- then
    slice it into 16x16x16 chunk sections, pack each section with numpy in
    the Minecraft 1.21 chunk format, and hand the finished chunks to
    anvil-parser2's region-file writer.
  - RAM stays bounded (~70 MB per region) regardless of total area.

This replaced an earlier version that called region.set_block() once per
block. Same output, but that approach managed only ~1,500 columns/sec,
which is ~12 hours for an 8 km x 8 km area at 1 m/block.
"""

import os

import numpy as np
import anvil
import anvil.empty_section  # noqa: F401  (patches nbt so long arrays are written unsigned)
from nbt import nbt
from tqdm import tqdm

from block_mapping import style_for, biome_for, CLIMATES
from terrain_config import TerrainConfig

REGION_SIZE = 512
WORLD_HEIGHT = 256  # we build in Y 0..255 (sections 0-15); the world itself spans -64..319

# Chunks are written in the Minecraft 1.21 format (DataVersion 3953): the
# modern layout with lowercase "sections"/"block_states", namespaced status,
# and packed longs that never straddle two longs. Newer versions (1.21.x,
# 26.x) upgrade 1.21 chunks on load exactly like they upgrade any 1.21
# player's world. (An earlier version wrote the old 1.14 format, which
# recent Minecraft versions silently discarded.)
CHUNK_DATA_VERSION = 3953
MIN_SECTION = -4  # overworld bottom is y=-64

# Leaves placed by a program must be marked persistent, or they decay
# away over time (the game thinks they're far from any log).
LEAF_PROPERTIES = {"persistent": "true", "distance": "1", "waterlogged": "false"}


def _properties_for(name):
    return LEAF_PROPERTIES if name.endswith("_leaves") else None


# --------------------------------------------------------------------------
# Block palette: every block this builder can place gets a small integer ID,
# so a whole region can live in one uint8 numpy array.
# --------------------------------------------------------------------------

class _Palette:
    def __init__(self):
        self.names = []
        self._ids = {}
        self.add("air")  # ID 0 must be air

    def add(self, name: str) -> int:
        if name not in self._ids:
            self._ids[name] = len(self.names)
            self.names.append(name)
        return self._ids[name]


class _Luts:
    """
    Lookup tables indexed [climate, land cover code] (see climate.py and
    block_mapping.py), so a whole region is styled with array indexing.
    """

    def __init__(self):
        self.pal = pal = _Palette()
        for name in ("stone", "water"):
            pal.add(name)
        shape = (max(CLIMATES) + 1, 256)
        self.surf = np.zeros(shape, dtype=np.uint8)
        self.sub = np.zeros(shape, dtype=np.uint8)
        self.water = np.zeros(shape, dtype=bool)
        self.forest = np.zeros(shape, dtype=bool)
        self.log = np.zeros(shape, dtype=np.uint8)
        self.leaves = np.zeros(shape, dtype=np.uint8)
        self.biome_names = sorted({biome_for(code, cl) for cl in CLIMATES for code in range(256)})
        self.biome = np.zeros(shape, dtype=np.uint8)
        for cl in CLIMATES:
            for code in range(256):
                s = style_for(code, cl)
                self.surf[cl, code] = pal.add(s.surface_block)
                self.sub[cl, code] = pal.add(s.subsurface_block)
                self.water[cl, code] = s.is_water
                self.forest[cl, code] = s.is_forest
                self.log[cl, code] = pal.add(f"{s.tree}_log")
                self.leaves[cl, code] = pal.add(f"{s.tree}_leaves")
                self.biome[cl, code] = self.biome_names.index(biome_for(code, cl))
        assert len(pal.names) < 256


# --------------------------------------------------------------------------
# Modern (1.18+) chunk NBT
# --------------------------------------------------------------------------

def pack_longs(indices: np.ndarray, bits: int) -> list:
    """
    Pack palette indices into 64-bit longs the 1.16+ way: floor(64/bits)
    entries per long, lowest bits first, never split across two longs.
    """
    per_long = 64 // bits
    n_longs = -(-len(indices) // per_long)
    padded = np.zeros(n_longs * per_long, dtype=np.uint64)
    padded[: len(indices)] = indices
    padded = padded.reshape(n_longs, per_long)
    shifts = (np.arange(per_long, dtype=np.uint64) * np.uint64(bits))
    return np.bitwise_or.reduce(padded << shifts, axis=1).tolist()


def _block_tag(name):
    tag = nbt.TAG_Compound()
    tag.tags.append(nbt.TAG_String(name="Name", value=f"minecraft:{name}"))
    props = _properties_for(name)
    if props:
        p = nbt.TAG_Compound()
        p.name = "Properties"
        for k, v in props.items():
            p.tags.append(nbt.TAG_String(name=k, value=v))
        tag.tags.append(p)
    return tag


def _section_tag(y, ids, pal_names, biome_cells, biome_names):
    """
    One 16x16x16 section. `ids` is indexed [y, z, x] (Minecraft's order
    y*256 + z*16 + x); `biome_cells` is the 4x4 [z, x] biome index grid.
    """
    sec = nbt.TAG_Compound()
    sec.tags.append(nbt.TAG_Byte(name="Y", value=y))

    flat = ids.reshape(-1)
    uniq = np.unique(flat)
    states = nbt.TAG_Compound()
    states.name = "block_states"
    palette = nbt.TAG_List(name="palette", type=nbt.TAG_Compound)
    for i in uniq:
        palette.tags.append(_block_tag(pal_names[i]))
    states.tags.append(palette)
    if len(uniq) > 1:
        lut = np.zeros(256, dtype=np.uint64)
        lut[uniq] = np.arange(len(uniq), dtype=np.uint64)
        data = nbt.TAG_Long_Array(name="data")
        data.value = pack_longs(lut[flat], max((len(uniq) - 1).bit_length(), 4))
        states.tags.append(data)
    sec.tags.append(states)

    sec.tags.append(_biomes_tag(biome_cells, biome_names))
    return sec


def _biomes_tag(biome_cells, biome_names):
    """Biomes are stored per 4x4x4 cell: 64 entries, index = y*16 + z*4 + x."""
    flat = np.tile(biome_cells.reshape(16), 4)
    uniq = np.unique(flat)
    biomes = nbt.TAG_Compound()
    biomes.name = "biomes"
    bpal = nbt.TAG_List(name="palette", type=nbt.TAG_String)
    for i in uniq:
        bpal.tags.append(nbt.TAG_String(value=f"minecraft:{biome_names[i]}"))
    biomes.tags.append(bpal)
    if len(uniq) > 1:
        lut = np.zeros(max(int(uniq.max()) + 1, 1), dtype=np.uint64)
        lut[uniq] = np.arange(len(uniq), dtype=np.uint64)
        data = nbt.TAG_Long_Array(name="data")
        data.value = pack_longs(lut[flat], (len(uniq) - 1).bit_length())
        biomes.tags.append(data)
    return biomes


class _ModernChunk:
    """A finished chunk; anvil-parser2's EmptyRegion calls .save() to write it."""
    __slots__ = ("x", "z", "sections")

    def __init__(self, x, z):
        self.x, self.z = x, z
        self.sections = []

    def save(self):
        root = nbt.NBTFile()
        root.tags.extend([
            nbt.TAG_Int(name="DataVersion", value=CHUNK_DATA_VERSION),
            nbt.TAG_Int(name="xPos", value=self.x),
            nbt.TAG_Int(name="yPos", value=MIN_SECTION),
            nbt.TAG_Int(name="zPos", value=self.z),
            nbt.TAG_String(name="Status", value="minecraft:full"),
            nbt.TAG_Long(name="LastUpdate", value=0),
            nbt.TAG_Long(name="InhabitedTime", value=0),
            # No light data is stored, so tell Minecraft to calculate it.
            nbt.TAG_Byte(name="isLightOn", value=0),
            nbt.TAG_List(name="block_entities", type=nbt.TAG_Compound),
        ])
        sections = nbt.TAG_List(name="sections", type=nbt.TAG_Compound)
        sections.tags.extend(self.sections)
        root.tags.append(sections)
        return root


# --------------------------------------------------------------------------
# Heights
# --------------------------------------------------------------------------

def compute_heights(elevation_grid: np.ndarray, config: TerrainConfig, mask=None) -> np.ndarray:
    """Convert real-world elevation (meters) into Minecraft Y levels."""
    relief = (elevation_grid - config.reference_sea_level_m) / config.vertical_meters_per_block
    y = config.sea_level_y + np.round(relief)
    lo = config.min_build_y + config.floor_margin
    hi = config.max_build_y - config.ceiling_margin
    counted = y if mask is None else y[mask]
    clipped_low = int((counted < lo).sum())
    clipped_high = int((counted > hi).sum())
    if clipped_low or clipped_high:
        print(f"[heights] clamped {clipped_low:,} columns below y={lo}, "
              f"{clipped_high:,} columns above y={hi}. Increase "
              f"vertical_meters_per_block to compress relief further if "
              f"this number is large.")
    y = np.clip(y, lo, hi).astype(np.int16)  # Y fits easily; half the memory of int32
    return y


# --------------------------------------------------------------------------
# Region volume
# --------------------------------------------------------------------------

def _fill_region_volume(top, lc, cl, config, luts):
    """
    Build a (WORLD_HEIGHT, 512, 512) uint8 volume of palette IDs for one
    region, indexed [y, z, x] in region-local coordinates.

    `top` (int32), `lc` (land cover) and `cl` (climate) are 512x512 arrays;
    columns outside the build area have top = -1.
    """
    STONE, WATER = luts.pal.add("stone"), luts.pal.add("water")

    floor_y = config.min_build_y + config.floor_margin
    sea = config.sea_level_y
    inside = top >= 0
    is_water = luts.water[cl, lc] & inside

    # Each column is described by 3 boundaries:
    #   [floor_y, a)  -> stone
    #   [a, b)        -> mid block
    #   [b, c]        -> top block
    # Land:  a = top-4 (at least floor), b = c = top; mid = subsurface, top = surface
    # Water: the water surface is the column's own height (a lake at 300 m
    #        stays at 300 m), or sea level if the DEM dips below it. Water
    #        is at least WATER_DEPTH blocks deep.
    #        a = lakebed, b = c = water surface; mid = water, top = water
    WATER_DEPTH = 3
    a = np.maximum(floor_y, top - 4)
    b = top.copy()
    c = top.copy()
    mid = luts.sub[cl, lc]
    topb = luts.surf[cl, lc]

    surface = np.maximum(top, sea)
    lakebed = np.maximum(floor_y, np.minimum(top, surface - WATER_DEPTH))
    a = np.where(is_water, lakebed, a)
    b = np.where(is_water, surface, b)
    c = np.where(is_water, surface, c)
    mid = np.where(is_water, WATER, mid).astype(np.uint8)
    topb = np.where(is_water, WATER, topb).astype(np.uint8)

    # Columns outside the build area: everything air.
    a = np.where(inside, a, -1)
    b = np.where(inside, b, -1)
    c = np.where(inside, c, -2)

    Y = np.arange(WORLD_HEIGHT, dtype=np.int16)[:, None, None]
    a, b, c = a.astype(np.int16), b.astype(np.int16), c.astype(np.int16)
    vol = np.zeros((WORLD_HEIGHT,) + top.shape, dtype=np.uint8)
    # Fill 16 rows at a time so the temporary true/false arrays stay small
    # (~2 MB each instead of 64 MB) -- keeps peak memory low on 8 GB PCs.
    STRIP = 16
    for z0 in range(0, top.shape[0], STRIP):
        zs = slice(z0, z0 + STRIP)
        if not inside[zs].any():
            continue
        v = vol[:, zs]
        sa, sb, sc = a[None, zs], b[None, zs], c[None, zs]
        # Write the layers bottom-up; later layers only touch their own range.
        np.copyto(v, np.uint8(STONE), where=(Y >= floor_y) & (Y < sa))
        np.copyto(v, mid[None, zs], where=(Y >= sa) & (Y < sb) & (Y >= floor_y))
        np.copyto(v, topb[None, zs], where=(Y >= sb) & (Y <= sc))
    return vol


def _place_trees(vol, top, lc, cl, config, luts, rng):
    """
    Simple trees on forest columns: 4-6 logs plus a leaf blob. The wood
    type comes from the local climate (oak, acacia in dry country, mangrove
    in warm wetlands).
    """
    AIR = 0
    candidates = luts.forest[cl, lc] & (top >= 0)
    chosen = candidates & (rng.random(top.shape) < config.tree_density)
    zs, xs = np.nonzero(chosen)
    heights = rng.integers(4, 7, size=len(zs))  # 4..6, same as before
    n, sz, sx = vol.shape[0], vol.shape[1], vol.shape[2]

    for z, x, h in zip(zs, xs, heights):
        LOG = luts.log[cl[z, x], lc[z, x]]
        LEAVES = luts.leaves[cl[z, x], lc[z, x]]
        base = int(top[z, x]) + 1
        if base + h > n:
            continue  # would poke out of the world; skip this tree
        vol[base:base + h, z, x] = LOG
        crown = base + h
        for dy in range(-2, 2):
            y = crown + dy
            if not (0 <= y < n):
                continue
            radius = 2 if dy < 1 else 1
            z0, z1 = max(z - radius, 0), min(z + radius + 1, sz)
            x0, x1 = max(x - radius, 0), min(x + radius + 1, sx)
            dz = np.arange(z0, z1)[:, None] - z
            dx = np.arange(x0, x1)[None, :] - x
            shape = (np.abs(dx) + np.abs(dz)) <= radius + 1
            layer = vol[y, z0:z1, x0:x1]
            # Leaves only fill air, so trunks (this tree's or a neighbor's)
            # are never overwritten.
            layer[shape & (layer == AIR)] = LEAVES


def _region_to_anvil(vol, biome_idx, rx, rz, pal, biome_names):
    """Slice a region volume into chunks/sections and build an EmptyRegion."""
    region = anvil.EmptyRegion(rx, rz)
    n_sections = vol.shape[0] // 16
    any_chunk = False

    # Quick per-chunk emptiness test: does any block in the 16x16 footprint exist?
    # (max over height first: avoids a 64 MB temporary true/false copy)
    occupied = vol.max(axis=0).reshape(32, 16, 32, 16).any(axis=(1, 3))  # [cz, cx]

    for cz, cx in zip(*np.nonzero(occupied)):
        z0, x0 = cz * 16, cx * 16
        # one biome per 4x4 column cell: sample the middle of each cell
        cells = biome_idx[z0 + 2:z0 + 16:4, x0 + 2:x0 + 16:4]
        chunk = _ModernChunk(rx * 32 + int(cx), rz * 32 + int(cz))

        for sy in range(n_sections):
            sec = vol[sy * 16:(sy + 1) * 16, z0:z0 + 16, x0:x0 + 16]
            if not sec.any():
                continue  # all air: Minecraft fills missing sections with air
            chunk.sections.append(_section_tag(sy, sec, pal.names, cells, biome_names))

        region.add_chunk(chunk)
        any_chunk = True

    return region if any_chunk else None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_world(elevation_grid, landcover_grid, config: TerrainConfig, mask=None, climate=None):
    """
    Main entry point: writes all region (.mca) files needed to cover the
    grid into config.world_region_folder.

    mask: optional boolean array, same shape as the grids. Only columns
    where it's True are built (e.g. the inside of a county outline);
    everything else is left empty.

    climate: optional uint8 array from climate.classify(); picks dry /
    alpine / warm-wetland looks per column. None = normal everywhere.
    """
    height, width = elevation_grid.shape
    total_columns = height * width
    if total_columns > config.max_columns:
        raise ValueError(
            f"Grid has {total_columns:,} columns, over max_columns="
            f"{config.max_columns:,}. Increase meters_per_block, shrink the "
            f"bbox, or raise max_columns deliberately if you know what "
            f"you're doing (see README sizing table)."
        )
    if config.max_build_y > WORLD_HEIGHT - 1 or config.min_build_y < 0:
        raise ValueError("This builder writes Y 0..255 only; keep min_build_y >= 0 "
                         "and max_build_y <= 255 in terrain_config.py.")

    os.makedirs(config.world_region_folder, exist_ok=True)

    y_grid = compute_heights(elevation_grid, config, mask)
    if mask is not None:
        y_grid[~mask] = -1  # -1 = "don't build this column"
    landcover_grid = landcover_grid.astype(np.uint8, copy=False)
    rng = np.random.default_rng(config.seed)

    luts = _Luts()

    ox, oz = config.origin_x, config.origin_z
    min_wx, min_wz = ox, oz
    max_wx, max_wz = ox + width - 1, oz + height - 1

    r_start_x, r_end_x = min_wx // REGION_SIZE, max_wx // REGION_SIZE
    r_start_z, r_end_z = min_wz // REGION_SIZE, max_wz // REGION_SIZE
    regions = [(rx, rz) for rz in range(r_start_z, r_end_z + 1)
               for rx in range(r_start_x, r_end_x + 1)]
    print(f"[build] {width}x{height} columns -> {len(regions)} region file(s)")

    written = 0
    for rx, rz in tqdm(regions, desc="regions", unit="region"):
        rx0, rz0 = rx * REGION_SIZE, rz * REGION_SIZE
        wx_lo, wx_hi = max(rx0, min_wx), min(rx0 + REGION_SIZE - 1, max_wx)
        wz_lo, wz_hi = max(rz0, min_wz), min(rz0 + REGION_SIZE - 1, max_wz)

        # Region-local 512x512 maps; -1 marks "outside the build area".
        top = np.full((REGION_SIZE, REGION_SIZE), -1, dtype=np.int32)
        lc = np.zeros((REGION_SIZE, REGION_SIZE), dtype=np.uint8)
        cl = np.zeros((REGION_SIZE, REGION_SIZE), dtype=np.uint8)
        lz0, lz1 = wz_lo - rz0, wz_hi - rz0 + 1
        lx0, lx1 = wx_lo - rx0, wx_hi - rx0 + 1
        gz0, gz1 = wz_lo - oz, wz_hi - oz + 1
        gx0, gx1 = wx_lo - ox, wx_hi - ox + 1
        top[lz0:lz1, lx0:lx1] = y_grid[gz0:gz1, gx0:gx1]
        lc[lz0:lz1, lx0:lx1] = landcover_grid[gz0:gz1, gx0:gx1]
        if climate is not None:
            cl[lz0:lz1, lx0:lx1] = climate[gz0:gz1, gx0:gx1]
        if (top < 0).all():
            continue  # region lies entirely outside the county outline

        vol = _fill_region_volume(top, lc, cl, config, luts)
        if config.place_trees:
            _place_trees(vol, top, lc, cl, config, luts, rng)

        region = _region_to_anvil(vol, luts.biome[cl, lc], rx, rz, luts.pal, luts.biome_names)
        del vol
        if region is not None:
            region.save(os.path.join(config.world_region_folder, f"r.{rx}.{rz}.mca"))
            written += 1

    print(f"[build] done. {written} region file(s) written to: {config.world_region_folder}")
