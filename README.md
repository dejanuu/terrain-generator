# Minecraft County Generator

**Currently still some unidentified blocks where stone blocks are placed, ocean is not included when shore is reached** 

Turn any US county into a Minecraft Java world, built from real elevation and land cover data.

Type a county name, and it builds the land inside that county's real borders: hills and valleys from satellite elevation data, plus forests, farmland, towns, lakes, rivers, wetlands and snow from satellite land cover maps.

<!-- Add a screenshot here once you've built one, e.g.: ![Howard County, MD](screenshots/howard.png) -->

## What it does

- **Any US county by name.** "Howard County, MD", "travis tx" and "Orleans Parish, Louisiana" all work. Typos get "did you mean" suggestions.
- **Real borders.** Only the land inside the official county outline is built.
- **Automatic scale.** It picks how many meters each block represents so the build finishes in minutes, and squeezes tall mountains to fit under the height limit.
- **Real surfaces.** Forests get trees, farmland, urban areas, sand, snow, wetlands and water come from the land cover map, with matching biomes.
- **Climate-aware look.** The same land looks different in different places: desert and dry scrub (Arizona, Nevada, west Texas) get coarse dirt, sand, acacia trees and savanna/desert biomes with no rain; peaks above the tree line become bare stone or snow; Gulf Coast wetlands become mangrove swamp. Worked out automatically from land cover, elevation and latitude.
- **Real-world location bar.** A bar at the top of the screen shows the nearest real town and your real latitude/longitude as you walk around ("Howard County, MD · near Columbia · 39.2036° N 76.8610° W"). It's a built-in data pack, so no mods are needed.
- **Fast.** Builds millions of columns per minute.

## Requirements

- Windows, macOS or Linux, with **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/); on Windows, tick "Add to PATH" during install)
- **Minecraft Java Edition 1.21 or newer** (including 26.x, whose new world folder layout is detected automatically)
- A free **OpenTopography API key** for elevation data (see step 2 below)

## Setup

1. **Download** this project (green "Code" button → "Download ZIP"), unzip it, open a terminal in the folder, and run:
   ```
   pip install -r requirements.txt
   ```
2. **Get a free API key** at [portal.opentopography.org](https://portal.opentopography.org): sign up, then go to *My Account → myOpenTopo Authorizations* and request a key. It's issued instantly.
3. **Make an empty world.** In Minecraft: *Create New World → World Type: Superflat → Customize → "The Void" preset*. Open it once, then quit to the title screen.

## Usage

On Windows, double-click **`run.bat`**. Otherwise run:

```
python main.py
```

It asks which county to build. The first time only, it also asks for your world name and API key, which it remembers.

You can also give everything on the command line:

```
python main.py --county "Howard County, MD"
python main.py --county "Travis, TX" --world MyTerrainWorld
python main.py --county "Orleans Parish, LA" --meters-per-block 4
```

### Going to a real place

```
python goto.py "39.2781, -77.0152"
python goto.py "Merriweather Post Pavilion, Columbia MD"
```

It prints the command to land on the ground at that spot (and copies it on Windows: press T in Minecraft, Ctrl+V, Enter). Get coordinates from Google Maps by right-clicking the map. It uses the last world you built into; add `--world "Name"` for another one.

When a build is done, open the world in Minecraft and run the `/spreadplayers` command it prints. It drops you safely on the ground in the middle of the county. Commands must be allowed in the world; use Creative mode to fly around.

### Options

| Option | What it does |
|---|---|
| `--county "NAME, ST"` | County to build |
| `--world NAME` | Minecraft world (from your saves folder) to build into |
| `--meters-per-block N` | Horizontal scale. Default `auto`. Smaller = bigger, more detailed world |
| `--vertical-meters-per-block N` | Height scale. Default `auto`. Bigger = flatter terrain |
| `--target-columns N` | Size target for `auto` scale (default 40,000,000) |
| `--no-trees` | Don't place trees |
| `--no-hud` | Don't add the real-world location bar |
| `--no-climate` | Same look everywhere (no desert / alpine / mangrove styles) |
| `--place "NAME"` / `--bbox W S E N` | Build any place on Earth by name or by coordinates instead of a county |

## Good to know

- **Scale:** a whole county at 1 block = 1 meter would be billions of blocks, so large counties are built at 4–10 m per block by default. Elevation data is about 30 m resolution, so going below ~5 m/block adds land cover detail but not terrain detail.
- **Connecticut** no longer has counties in Census data. Use its planning regions instead, e.g. "Capitol Planning Region, CT".
- **Downloads are cached** in `work/`, so rebuilding the same county at another scale is quick.
- Builds use the classic Y 0–255 height range.
- **Minimap:** for a map in the corner of the screen, add a minimap mod such as [Xaero's Minimap](https://modrinth.com/mod/xaeros-minimap). Since the world matches the real land, its minimap reads like a real map of the county.
- **"Failed to load world":** always build into a world you created in Minecraft and opened at least once. The generator checks for this and refuses to build into a world with a damaged `level.dat`.

## How it works

1. Finds the county outline in the US Census Bureau boundary file (downloaded once).
2. Downloads elevation (SRTM, ~30 m) from OpenTopography and land cover (ESA WorldCover 2021, 10 m) from Microsoft Planetary Computer.
3. Projects both onto one grid in meters (UTM) at the chosen scale.
4. Converts each column to blocks and writes Minecraft region (`.mca`) files directly into your world.

## Credits and data sources

- Elevation: NASA SRTM, via OpenTopography. *This work is based on API services provided by the OpenTopography Facility with support from the National Science Foundation under NSF Award Numbers 2410799, 2410800 & 2410801.*
- Land cover: © ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium. Accessed via Microsoft Planetary Computer.
- County boundaries: US Census Bureau, 2023 Cartographic Boundary Files.
- Town names: US Census Bureau, 2023 Gazetteer Files.
- Minecraft region writing: [anvil-parser2](https://pypi.org/project/anvil-parser2/).

Not an official Minecraft product. Not approved by or associated with Mojang or Microsoft.

## License

MIT. See [LICENSE](LICENSE).
