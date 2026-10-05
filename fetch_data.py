"""
Fetches real-world elevation and land cover data for a bounding box, and
aligns them onto a single common grid at the resolution you want (one
sample per Minecraft column).

Elevation source: OpenTopography's Global DEM API, serving SRTM GL1
(~30m resolution worldwide, 60N-56S). Plain HTTPS GET returning a
GeoTIFF directly -- no external CLI tools required, so it works the
same on Windows/Mac/Linux. Free API key: https://portal.opentopography.org

Land cover source: ESA WorldCover 10m (2021), fetched through Microsoft's
Planetary Computer STAC catalog (public, no account needed).
"""

import os
import math

import numpy as np
import requests
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.enums import Resampling as ResamplingEnum

import pystac_client
import planetary_computer


def _utm_epsg_for(lon: float, lat: float) -> int:
    """Pick a sensible UTM CRS so our grid is in real meters."""
    zone = int(math.floor((lon + 180) / 6) + 1)
    return (32600 if lat >= 0 else 32700) + zone


def fetch_elevation(bbox, out_path, api_key, demtype="SRTMGL1"):
    """
    Download an elevation GeoTIFF covering bbox = (west, south, east, north)
    from OpenTopography's Global DEM API and save it to out_path.

    Get a free API key at https://portal.opentopography.org
    (sign up, then "My Account" -> "myOpenTopo Authorizations" -> request
    a key -- it's issued instantly, no approval wait).
    """
    if not api_key:
        raise RuntimeError(
            "No OpenTopography API key provided. Get a free one at "
            "https://portal.opentopography.org (My Account -> myOpenTopo "
            "Authorizations), then pass it with --opentopo-api-key or set "
            "the OPENTOPOGRAPHY_API_KEY environment variable."
        )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    # Ask for ~1 km extra on every side, so the edges of the area have
    # real data to interpolate from.
    pad = 0.01
    west, south, east, north = bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad
    print(f"[elevation] requesting {demtype} DEM for bbox {bbox} from OpenTopography ...")

    resp = requests.get(
        "https://portal.opentopography.org/API/globaldem",
        params={
            "demtype": demtype,
            "west": west,
            "south": south,
            "east": east,
            "north": north,
            "outputFormat": "GTiff",
            "API_Key": api_key,
        },
        timeout=180,
    )
    if resp.status_code != 200 or resp.headers.get("content-type", "").startswith("application/json"):
        # API returns JSON on errors (bad key, area too large, etc.)
        raise RuntimeError(f"OpenTopography request failed: {resp.status_code} {resp.text[:500]}")

    with open(out_path, "wb") as f:
        f.write(resp.content)

    print(f"[elevation] saved -> {out_path}")
    return out_path


def fetch_landcover(bbox, out_path, year=2021, target_res_m=10.0):
    """
    Query the ESA WorldCover 10m collection on Planetary Computer, download
    only the part of each tile that covers bbox, mosaic, and save as GeoTIFF.

    target_res_m: resolution to download at. For zoomed-out builds (e.g.
    20 m per block) there's no point pulling full 10 m data for a whole
    county, so it reads the tiles' built-in lower-resolution overviews.
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    print("[landcover] querying ESA WorldCover via Planetary Computer STAC ...")

    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )

    # The collection holds BOTH the 2020 (v1) and 2021 (v2) maps for every
    # tile, so filter to one year -- otherwise every tile is downloaded
    # twice and the two editions get blended together.
    search = catalog.search(
        collections=["esa-worldcover"],
        bbox=list(bbox),
        datetime=f"{year}-01-01/{year}-12-31",
    )
    items = list(search.items())
    if not items:
        raise RuntimeError(f"No ESA WorldCover {year} tiles found for that bbox.")

    print(f"[landcover] found {len(items)} tile(s), downloading the area at ~{target_res_m:g} m ...")
    return mosaic_clip([item.assets["map"].href for item in items], bbox, out_path,
                       target_res_m=target_res_m)


def mosaic_clip(hrefs, bbox, out_path, buffer_deg=0.002, target_res_m=10.0):
    """
    Read just the bbox window (+ a small buffer so reprojection has data
    right up to the edges) from each source raster, optionally at reduced
    resolution, then merge the pieces.

    Each WorldCover tile is 3x3 degrees = 36,000 x 36,000 pixels (~1.3 GB
    uncompressed). Windowed reads mean only the needed part is downloaded
    from the cloud-optimized GeoTIFF, instead of whole tiles.
    """
    from affine import Affine
    from rasterio.io import MemoryFile
    from rasterio.merge import merge
    from rasterio.windows import from_bounds

    west, south, east, north = bbox
    b = buffer_deg
    clip = (west - b, south - b, east + b, north + b)
    target_res_deg = target_res_m / 111_320.0

    memfiles, pieces = [], []
    for href in hrefs:
        with rasterio.open(href) as src:
            l, bt, r, t = src.bounds
            inter = (max(l, clip[0]), max(bt, clip[1]), min(r, clip[2]), min(t, clip[3]))
            if inter[0] >= inter[2] or inter[1] >= inter[3]:
                continue  # tile doesn't overlap the area
            win = from_bounds(*inter, transform=src.transform).round_offsets().round_lengths()
            if win.width < 1 or win.height < 1:
                continue
            factor = max(1, int(target_res_deg / abs(src.res[0])))
            out_w, out_h = max(1, int(win.width) // factor), max(1, int(win.height) // factor)
            data = src.read(1, window=win, out_shape=(out_h, out_w),
                            resampling=ResamplingEnum.nearest)  # categories: never average
            transform = src.window_transform(win) * Affine.scale(win.width / out_w, win.height / out_h)
            profile = dict(driver="GTiff", width=out_w, height=out_h, count=1,
                           dtype=data.dtype, crs=src.crs, transform=transform, nodata=0)
        mf = MemoryFile()
        with mf.open(**profile) as dst:
            dst.write(data, 1)
        memfiles.append(mf)
        pieces.append(mf.open())

    if not pieces:
        raise RuntimeError("Land cover tiles were found, but none overlap the bbox.")

    mosaic, transform = merge(pieces, bounds=clip, nodata=0)
    profile = dict(driver="GTiff", width=mosaic.shape[2], height=mosaic.shape[1], count=1,
                   dtype=mosaic.dtype, crs=pieces[0].crs, transform=transform, nodata=0,
                   compress="deflate")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(mosaic)
    for p in pieces:
        p.close()
    for mf in memfiles:
        mf.close()

    print(f"[landcover] saved {mosaic.shape[2]} x {mosaic.shape[1]} px -> {out_path}")
    return out_path


def county_mask(geometry, transform, width, height, dst_crs):
    """
    Boolean grid: True for columns inside the county outline (GeoJSON
    geometry in lon/lat), on the same grid align_rasters() returns.
    """
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom

    geom = transform_geom("EPSG:4326", dst_crs, geometry)
    mask = rasterize([(geom, 1)], out_shape=(height, width), transform=transform,
                     fill=0, all_touched=True, dtype="uint8")
    return mask.astype(bool)


def grid_for_bbox(bbox, meters_per_block):
    """
    The build grid: a UTM grid in meters at `meters_per_block`, covering
    bbox. Sized from the bbox itself (lon/lat), NOT from the elevation
    file's bounds -- the downloaded DEM can extend past the bbox. goto.py
    uses this same function, so its coordinates always match the build.
    Returns (transform, width, height, crs).
    """
    west, south, east, north = bbox
    dst_crs = f"EPSG:{_utm_epsg_for((west + east) / 2, (south + north) / 2)}"
    transform, width, height = calculate_default_transform(
        "EPSG:4326", dst_crs, 1000, 1000,
        left=west, bottom=south, right=east, top=north,
        resolution=meters_per_block,
    )
    return transform, width, height, dst_crs


def align_rasters(elevation_path, landcover_path, bbox, meters_per_block):
    """
    Reproject + resample both rasters onto one common UTM grid at
    `meters_per_block` resolution, covering bbox exactly.

    Returns: (elevation_grid, landcover_grid, transform, crs, width, height)
    elevation_grid: float32 numpy array, meters
    landcover_grid: uint8 numpy array, ESA WorldCover class codes
    """
    west, south, east, north = bbox
    center_lon = (west + east) / 2
    center_lat = (south + north) / 2
    transform, width, height, dst_crs = grid_for_bbox(bbox, meters_per_block)

    with rasterio.open(elevation_path) as src:
        # Start as "unknown" (NaN) rather than 0 m: cells the DEM doesn't
        # reach would otherwise become sea level, which in the mountains is
        # a pit a kilometer deep.
        elevation_grid = np.full((height, width), np.nan, dtype=np.float32)
        reproject(
            source=rasterio.band(src, 1),
            destination=elevation_grid,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata if src.nodata is not None else -32768,
            dst_transform=transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

    # Fill any gaps (DEM voids, no-data, edges) from the nearest real values.
    valid = np.isfinite(elevation_grid) & (elevation_grid > -500)
    if not valid.all():
        if valid.any():
            from rasterio.fill import fillnodata
            elevation_grid[~valid] = 0.0
            elevation_grid = fillnodata(elevation_grid, mask=valid.astype(np.uint8),
                                        max_search_distance=float(max(width, height)),
                                        smoothing_iterations=0)
            print(f"[align] filled {int((~valid).sum()):,} elevation cells with no data from their neighbors")
        else:
            elevation_grid[:] = 0.0

    with rasterio.open(landcover_path) as src:
        landcover_grid = np.zeros((height, width), dtype=np.uint8)
        reproject(
            source=rasterio.band(src, 1),
            destination=landcover_grid,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=ResamplingEnum.nearest,  # categorical data: no averaging
        )

    print(f"[align] common grid: {width} x {height} cells "
          f"({width * height:,} columns) at {meters_per_block} m/px, CRS={dst_crs}")

    return elevation_grid, landcover_grid, transform, dst_crs, width, height