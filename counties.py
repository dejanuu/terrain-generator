"""
Look up a US county by name and return its exact outline.

Uses the Census Bureau's 2023 cartographic boundary file (all 3,235
counties and equivalents, 1:500,000 scale). It's downloaded once (~12 MB)
and cached, so later runs work offline.

Accepted name formats (case, apostrophes and periods don't matter):
    "Prince George's County, MD"      "prince georges county maryland"
    "Travis County, Texas"            "Travis, TX"
    "Orleans Parish, LA"              "Anchorage, AK"
    "Baltimore city, MD"   (independent cities are separate from counties)
"""

import difflib
import io
import os
import re
import unicodedata
import zipfile

CENSUS_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"
ZIP_NAME = "cb_2023_us_county_500k.zip"

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico", "GU": "Guam",
    "VI": "U.S. Virgin Islands", "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}

# Words that just say "this is a county", so "Travis" == "Travis County".
_SUFFIXES = [
    "city and borough", "census area", "planning region", "municipality",
    "county", "parish", "borough", "municipio",
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)                 # Doña -> Dona
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'")
    s = re.sub(r"[.'`]", "", s)          # Prince George's -> prince georges
    s = re.sub(r"\bsaint\b", "st", s)    # Saint Louis -> st louis
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_suffix(s: str) -> str:
    for suf in _SUFFIXES:
        if s.endswith(" " + suf):
            return s[: -len(suf) - 1]
    return s


def _parse_state(text: str):
    """Return a 2-letter code for a state name/abbreviation, or None."""
    t = _norm(text)
    for code, name in STATES.items():
        if t == code.lower() or t == _norm(name):
            return code
    return None


def _split_query(query: str):
    """'Travis County, Texas' -> ('travis county', 'TX'). State is optional."""
    if "," in query:
        name, state_txt = query.rsplit(",", 1)
        state = _parse_state(state_txt)
        if state is None:
            raise LookupError(f"Didn't recognize '{state_txt.strip()}' as a US state.")
        return _norm(name), state
    # No comma: allow a trailing state name/abbreviation ("Travis County TX").
    words = _norm(query).split()
    for n in (3, 2, 1):  # "district of columbia", "new york", "tx"
        if len(words) > n:
            state = _parse_state(" ".join(words[-n:]))
            if state:
                return " ".join(words[:-n]), state
    return _norm(query), None


# ---------------------------------------------------------------------------
# Loading the Census file
# ---------------------------------------------------------------------------

def _zip_path(cache_dir):
    return os.path.join(cache_dir, ZIP_NAME)


def _ensure_downloaded(cache_dir):
    path = _zip_path(cache_dir)
    if os.path.exists(path):
        return path
    import requests

    os.makedirs(cache_dir, exist_ok=True)
    print(f"[county] downloading US county boundaries from the Census Bureau (one time, ~12 MB) ...")
    try:
        resp = requests.get(CENSUS_URL, timeout=180,
                            headers={"User-Agent": "Mozilla/5.0 (terrain-to-minecraft)"})
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            f"Couldn't download the county boundary file ({e}).\n"
            f"Download it yourself in a browser from:\n  {CENSUS_URL}\n"
            f"and put the .zip (don't unzip it) here:\n  {os.path.abspath(path)}"
        ) from e
    with open(path + ".part", "wb") as f:
        f.write(resp.content)
    os.replace(path + ".part", path)
    return path


def _open_reader(cache_dir):
    import shapefile  # pyshp

    zf = zipfile.ZipFile(_ensure_downloaded(cache_dir))
    base = [n for n in zf.namelist() if n.endswith(".shp")][0][:-4]
    return shapefile.Reader(
        shp=io.BytesIO(zf.read(base + ".shp")),
        shx=io.BytesIO(zf.read(base + ".shx")),
        dbf=io.BytesIO(zf.read(base + ".dbf")),
        encoding="utf-8", encodingErrors="replace",
    )


def _records(reader):
    fields = [f[0] for f in reader.fields[1:]]
    for i, rec in enumerate(reader.records()):
        r = dict(zip(fields, rec))
        state = r.get("STUSPS") or ""
        yield i, r.get("NAME", ""), r.get("NAMELSAD", r.get("NAME", "")), state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class County:
    def __init__(self, full_name, state, bbox, geometry):
        self.full_name = full_name      # e.g. "Prince George's County"
        self.state = state              # e.g. "MD"
        self.bbox = bbox                # (west, south, east, north), degrees
        self.geometry = geometry        # GeoJSON-style dict, lon/lat

    @property
    def label(self):
        return f"{self.full_name}, {self.state}"

    @property
    def slug(self):
        return re.sub(r"[^a-z0-9]+", "_", _norm(self.label)).strip("_")


def find_county(query: str, cache_dir: str) -> County:
    """
    Return the County matching `query`, or raise LookupError with
    suggestions if it's ambiguous or not found.
    """
    want, state = _split_query(query)
    want_bare = _strip_suffix(want)
    reader = _open_reader(cache_dir)
    rows = [r for r in _records(reader) if state is None or r[3] == state]

    # 1) exact full-name match ("baltimore county" vs "baltimore city")
    hits = [r for r in rows if _norm(r[2]) == want]
    # 2) bare-name match ("travis" -> Travis County)
    if not hits:
        hits = [r for r in rows if _norm(r[1]) == want_bare]
        # "Baltimore, MD" matches both the county and the city: prefer the county
        if len(hits) > 1 and state:
            county_hits = [r for r in hits if _norm(r[2]).endswith(" county")]
            if len(county_hits) == 1:
                hits = county_hits

    if len(hits) == 1:
        i, _, full, st = hits[0]
        shape = reader.shape(i)
        return County(full, st, tuple(shape.bbox), shape.__geo_interface__)

    if len(hits) > 1:
        options = "\n  ".join(sorted(f"{r[2]}, {r[3]}" for r in hits)[:40])
        raise LookupError(f"'{query}' matches {len(hits)} places. Add the state, e.g.:\n  {options}")

    # Not found: suggest close spellings -- in the given state first, then
    # anywhere (catches a right name with the wrong state).
    def suggestions(pool):
        choices = {f"{_norm(r[2])}|{r[3]}": f"{r[2]}, {r[3]}" for r in pool}
        keys = [k.split("|")[0] for k in choices]
        close = difflib.get_close_matches(want, keys, n=8, cutoff=0.6)
        return sorted({v for k, v in choices.items() if k.split("|")[0] in close})

    sugg = suggestions(rows)
    if not sugg and state is not None:
        sugg = suggestions(list(_records(reader)))
    msg = f"No county named '{query}' found."
    if state == "CT":
        msg += (" Note: since 2022 the Census uses Connecticut's nine planning "
                "regions instead of its old counties.")
    if sugg:
        msg += "\nDid you mean:\n  " + "\n  ".join(sugg)
    raise LookupError(msg)
