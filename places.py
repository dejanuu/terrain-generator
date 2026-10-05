"""
US town / city / CDP names with their center points, from the Census
Bureau's 2023 Gazetteer places file (~1 MB download, cached in work/).

Used by the location HUD to show "near <town>" in-game.
"""

import csv
import io
import os
import re
import zipfile

GAZ_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
           "2023_Gazetteer/2023_Gaz_place_national.zip")
ZIP_NAME = "2023_Gaz_place_national.zip"

# Census names end in a type word: "Columbia CDP", "Laurel city",
# "Nashville-Davidson metropolitan government (balance)". Strip it.
_SUFFIX = re.compile(
    r"\s+("
    r"(consolidated|metropolitan|metro|unified|urban county) government( \(balance\))?"
    r"|city and borough|corporation|comunidad|zona urbana|municipality"
    r"|CDP|city|town|township|village|borough"
    r")( \(balance\))?$",
    re.IGNORECASE,
)


def clean_name(name: str) -> str:
    cleaned = _SUFFIX.sub("", name.strip())
    return cleaned or name.strip()


def _ensure_downloaded(cache_dir):
    path = os.path.join(cache_dir, ZIP_NAME)
    if os.path.exists(path):
        return path
    import requests

    os.makedirs(cache_dir, exist_ok=True)
    print("[places] downloading US town names from the Census Bureau (one time, ~1 MB) ...")
    resp = requests.get(GAZ_URL, timeout=120,
                        headers={"User-Agent": "Mozilla/5.0 (terrain-to-minecraft)"})
    resp.raise_for_status()
    with open(path + ".part", "wb") as f:
        f.write(resp.content)
    os.replace(path + ".part", path)
    return path


def load_places(state: str, cache_dir: str):
    """
    Return [(name, lat, lon, land_area_m2), ...] for every Census place in
    `state` (2-letter code).
    """
    zf = zipfile.ZipFile(_ensure_downloaded(cache_dir))
    member = [n for n in zf.namelist() if n.lower().endswith(".txt")][0]
    text = zf.read(member).decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    col = {h: i for i, h in enumerate(header)}

    out = []
    for row in reader:
        if len(row) < len(header) or row[col["USPS"]].strip() != state:
            continue
        try:
            out.append((
                clean_name(row[col["NAME"]]),
                float(row[col["INTPTLAT"]]),
                float(row[col["INTPTLONG"]]),
                float(row[col["ALAND"]] or 0),
            ))
        except ValueError:
            continue
    return out
