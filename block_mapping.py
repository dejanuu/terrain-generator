"""
Maps ESA WorldCover land cover classes to Minecraft blocks and biomes.

WorldCover class codes (2021 v200):
  10 Tree cover        20 Shrubland          30 Grassland
  40 Cropland          50 Built-up           60 Bare/sparse vegetation
  70 Snow and ice      80 Permanent water    90 Herbaceous wetland
  95 Mangroves        100 Moss and lichen

The same land cover looks different depending on the local climate (see
climate.py): e.g. shrubland is green grass in Maryland but coarse dirt in
a savanna biome in Arizona. Each climate below only lists what it changes;
anything not listed falls back to the NORMAL look.

All names are bare Minecraft IDs; "minecraft:" is added when writing.
"""

from dataclasses import dataclass

from climate import NORMAL, DRY, ALPINE_ROCK, ALPINE_SNOW, WARM_WET


@dataclass
class SurfaceStyle:
    surface_block: str        # top block of terrain
    subsurface_block: str     # 2-4 blocks below surface
    is_water: bool = False
    is_forest: bool = False   # place trees here
    tree: str = "oak"         # wood type for trees: oak, acacia, mangrove, ...


# ---------------------------------------------------------------- NORMAL ---
LANDCOVER_STYLES = {
    10:  SurfaceStyle("grass_block", "dirt", is_forest=True),
    20:  SurfaceStyle("grass_block", "dirt"),
    30:  SurfaceStyle("grass_block", "dirt"),
    40:  SurfaceStyle("farmland", "dirt"),
    50:  SurfaceStyle("smooth_stone", "stone"),      # built-up/urban
    60:  SurfaceStyle("sand", "sandstone"),          # bare/sparse
    70:  SurfaceStyle("snow_block", "packed_ice"),   # snow/ice
    80:  SurfaceStyle("water", "water", is_water=True),
    90:  SurfaceStyle("grass_block", "mud"),         # wetland
    95:  SurfaceStyle("mangrove_roots", "mud", is_forest=True),
    100: SurfaceStyle("mossy_cobblestone", "stone"),
}
DEFAULT_STYLE = SurfaceStyle("grass_block", "dirt")

LANDCOVER_BIOMES = {
    10: "forest",
    70: "snowy_plains",
    80: "river",
    90: "swamp",
    95: "swamp",
}
DEFAULT_BIOME = "plains"

# ----------------------------------------------------- climate overrides ---
_ALL_LAND = (10, 20, 30, 40, 50, 60, 90, 95, 100)

CLIMATE_STYLES = {
    DRY: {   # desert & dry scrubland (Arizona, Nevada, west Texas, ...)
        10: SurfaceStyle("grass_block", "dirt", is_forest=True, tree="acacia"),
        20: SurfaceStyle("coarse_dirt", "dirt"),
        60: SurfaceStyle("sand", "sandstone"),
        100: SurfaceStyle("sand", "sandstone"),
    },
    ALPINE_ROCK: {code: SurfaceStyle("stone", "stone") for code in _ALL_LAND},
    ALPINE_SNOW: {code: SurfaceStyle("snow_block", "stone") for code in _ALL_LAND},
    WARM_WET: {
        90: SurfaceStyle("mud", "mud"),
        95: SurfaceStyle("mangrove_roots", "mud", is_forest=True, tree="mangrove"),
    },
}

CLIMATE_BIOMES = {
    # savanna & desert: no rain, dry-grass colors
    DRY: {10: "savanna", 20: "savanna", 30: "savanna", 40: "savanna",
          50: "desert", 60: "desert", 100: "desert", None: "desert"},
    ALPINE_ROCK: {**{c: "stony_peaks" for c in _ALL_LAND}, 70: "snowy_slopes", None: "stony_peaks"},
    ALPINE_SNOW: {**{c: "snowy_slopes" for c in _ALL_LAND}, 70: "snowy_slopes", 80: "frozen_river",
                  None: "snowy_slopes"},
    WARM_WET: {90: "mangrove_swamp", 95: "mangrove_swamp"},
}


def style_for(landcover_code: int, climate: int = NORMAL) -> SurfaceStyle:
    code = int(landcover_code)
    override = CLIMATE_STYLES.get(climate, {})
    if code in override:
        return override[code]
    return LANDCOVER_STYLES.get(code, DEFAULT_STYLE)


def biome_for(landcover_code: int, climate: int = NORMAL) -> str:
    code = int(landcover_code)
    override = CLIMATE_BIOMES.get(climate, {})
    if code in override:
        return override[code]
    if code not in LANDCOVER_BIOMES and None in override:
        return override[None]       # climate's default for unlisted land
    return LANDCOVER_BIOMES.get(code, DEFAULT_BIOME)


CLIMATES = (NORMAL, DRY, ALPINE_ROCK, ALPINE_SNOW, WARM_WET)
