# Dirt Finder

CLI-first geospatial MVP for finding reconnaissance candidate sites for a dirt-bike park
within roughly one hour of Hangzhou.

The pipeline uses open data where practical:

- OpenStreetMap via OSMnx for the Hangzhou boundary, drivable roads, and local drive-time
  isochrone.
- NASADEM/SRTM-style DEM downloads through `earthaccess`, or a manual DEM GeoTIFF path.
- ESA WorldCover 2021 land-cover tiles, or a manual land-cover GeoTIFF path.

Results are planning leads only. They do not imply ownership, buildability, permission, or legal
access.

## Setup

```bash
uv sync --extra dev
```

Auto DEM download requires NASA Earthdata credentials:

```bash
export EARTHDATA_USERNAME="..."
export EARTHDATA_PASSWORD="..."
```

Large geospatial downloads can take time. To avoid them, set `paths.dem_path` and
`paths.landcover_path` in the generated config to local GeoTIFFs.

## Usage

```bash
uv run dirt-finder init --preset hangzhou --output configs/hangzhou.toml
uv run dirt-finder run --config configs/hangzhou.toml
```

The full run writes:

- `outputs/candidates.geojson`
- `outputs/candidates.csv`
- `outputs/map.html`
- `outputs/run_metadata.json`

Individual stages are also available:

```bash
uv run dirt-finder fetch --config configs/hangzhou.toml
uv run dirt-finder analyze --config configs/hangzhou.toml
uv run dirt-finder render --config configs/hangzhou.toml
```

## Tests

```bash
uv run pytest
```

Live geospatial smoke tests are skipped unless explicitly enabled:

```bash
RUN_LIVE_GEO_TESTS=1 uv run pytest tests/test_live_smoke.py
```
