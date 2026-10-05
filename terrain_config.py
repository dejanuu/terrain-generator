"""
Configuration for a terrain-to-Minecraft build.

Everything that controls scale, height mapping, and file locations lives here
so you can tweak a run without touching pipeline code.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class TerrainConfig:
    # ---- Area of interest ----
    # (west_lon, south_lat, east_lon, north_lat), e.g. Rhode Island-ish box:
    # (-71.9, 41.1, -71.1, 42.0)
    bbox: tuple

    # ---- Scale ----
    # How many real-world meters map to ONE Minecraft block, horizontally.
    # 1.0 = true 1:1. Use higher values (2, 4, 8...) for large areas so the
    # world stays a manageable number of blocks. See README sizing table.
    meters_per_block: float = 1.0

    # How many real-world meters of ELEVATION map to one block of Y height.
    # Independent from meters_per_block so you can flatten dramatic relief
    # (mountains) without shrinking the map footprint. 1.0 = true 1:1 height.
    vertical_meters_per_block: float = 1.0

    # ---- Vertical placement ----
    # Elevation (in meters) that should land on Minecraft's sea level.
    # Usually 0 (mean sea level).
    reference_sea_level_m: float = 0.0

    # NOTE ON WORLD HEIGHT: the `anvil-parser2` library used here writes
    # the classic 0-255 vertical range (16 sections), not the 1.18+
    # extended range (-64 to 319). This is a limitation of the file-writer
    # library, not of Minecraft itself -- chunks in the 0-255 range work
    # fine in any world version, you just don't get the extra 64 blocks of
    # depth/height that 1.18+ added. If you need those, see the README
    # section "Extending to full -64..319 height" for the section-count
    # tweak required.
    min_build_y: int = 0
    max_build_y: int = 255
    sea_level_y: int = 64

    # Leave a bedrock/buffer margin at the very bottom so we don't paint
    # over bedrock, and a margin at the top so mountains don't clip abruptly.
    floor_margin: int = 5
    ceiling_margin: int = 4

    # ---- Trees / decoration ----
    place_trees: bool = True
    tree_density: float = 0.06  # probability per forest tile

    # ---- World save ----
    # Path to an EXISTING Minecraft world's "region" folder. Create the
    # world in-game first (recommended type: Superflat / Void preset),
    # then point this at:
    #   %appdata%\.minecraft\saves\<WorldName>\region
    world_region_folder: str = r"C:\Users\YOURNAME\AppData\Roaming\.minecraft\saves\MyTerrainWorld\region"

    # World-space origin: where bbox's NORTH-WEST corner lands in Minecraft
    # block coordinates. The map extends toward +x (east) and +z (south),
    # which matches Minecraft's compass. Keep at (0, 0) unless you're
    # stitching multiple tiles together.
    origin_x: int = 0
    origin_z: int = 0

    # ---- Working files ----
    work_dir: str = "./work"
    elevation_tif: str = "./work/elevation.tif"
    landcover_tif: str = "./work/landcover.tif"

    # Random seed for tree placement reproducibility
    seed: int = 1234

    # Cap on total columns to build in one run, as a safety valve so a
    # mistaken huge bbox doesn't try to write a trillion blocks. Raise this
    # deliberately once you've read the sizing section in the README.
    max_columns: int = 200_000_000
