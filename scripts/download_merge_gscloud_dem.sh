#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ -f "$REPO_ROOT/.env.gscloud" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env.gscloud"
  set +a
fi

GSCLOUD_FTP_URL="${GSCLOUD_FTP_URL:-ftp://ftp.gscloud.cn}"
GSCLOUD_FTP_URL="${GSCLOUD_FTP_URL%/}"
GSCLOUD_FTP_USER="${GSCLOUD_FTP_USER:-}"
GSCLOUD_FTP_PASS="${GSCLOUD_FTP_PASS:-}"
RAW_DIR="${RAW_DIR:-data/cache/dem/raw/gscloud}"
EXTRACT_DIR="${EXTRACT_DIR:-$RAW_DIR/extracted}"
OUTPUT="${OUTPUT:-data/cache/dem/dem_mosaic.tif}"

TILES=(
  ASTGTM_N30E120
  ASTGTM_N30E119
  ASTGTM_N29E120
  ASTGTM_N29E119
)

if [ -z "$GSCLOUD_FTP_USER" ] || [ -z "$GSCLOUD_FTP_PASS" ]; then
  cat >&2 <<'EOF'
Set GSCLOUD_FTP_USER and GSCLOUD_FTP_PASS, or create .env.gscloud in the repo root:

GSCLOUD_FTP_USER="..."
GSCLOUD_FTP_PASS="..."
EOF
  exit 2
fi

for command in curl unzip uv; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 127
  fi
done

mkdir -p "$RAW_DIR" "$EXTRACT_DIR" "$(dirname "$OUTPUT")"

FTP_HOST="${GSCLOUD_FTP_URL#ftp://}"
FTP_HOST="${FTP_HOST%%/*}"
NETRC_FILE="$(mktemp)"
trap 'rm -f "$NETRC_FILE"' EXIT
chmod 600 "$NETRC_FILE"
printf 'machine %s\nlogin %s\npassword %s\n' \
  "$FTP_HOST" "$GSCLOUD_FTP_USER" "$GSCLOUD_FTP_PASS" > "$NETRC_FILE"

for tile in "${TILES[@]}"; do
  archive="${tile}.img.zip"
  target="$RAW_DIR/$archive"
  if [ -s "$target" ] && unzip -tq "$target" >/dev/null 2>&1; then
    echo "valid $target"
    continue
  fi

  rm -f "$target"
  echo "download $archive"
  # The GSCloud FTP server returns an incorrect SIZE value for these files,
  # so curl must ignore the advertised length and read until transfer close.
  curl -fL --ignore-content-length --ftp-pasv \
    --netrc-file "$NETRC_FILE" \
    --output "$target" \
    "$GSCLOUD_FTP_URL/$archive"
  unzip -tq "$target" >/dev/null
done

for archive in "${TILES[@]}"; do
  unzip -oq "$RAW_DIR/${archive}.img.zip" -d "$EXTRACT_DIR"
done

uv run python - "$EXTRACT_DIR" "$OUTPUT" "$GSCLOUD_FTP_URL" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import rasterio
from rasterio.merge import merge

extract_dir = Path(sys.argv[1])
output = Path(sys.argv[2])
source_url = sys.argv[3]
sources = sorted(extract_dir.glob("ASTGTM_*.img"))

if len(sources) != 4:
    raise SystemExit(f"Expected 4 extracted .img tiles, found {len(sources)} in {extract_dir}")

datasets = [rasterio.open(path) for path in sources]
try:
    crs_values = {str(dataset.crs) for dataset in datasets}
    if len(crs_values) != 1:
        raise SystemExit(f"Input DEM tiles use mixed CRS values: {sorted(crs_values)}")

    mosaic, transform = merge(datasets, nodata=32767)
    profile = datasets[0].profile.copy()
    profile.update(
        driver="GTiff",
        height=mosaic.shape[1],
        width=mosaic.shape[2],
        transform=transform,
        count=mosaic.shape[0],
        compress="deflate",
        tiled=True,
        bigtiff="if_safer",
        nodata=32767,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(output, "w", **profile) as destination:
        destination.write(mosaic)
finally:
    for dataset in datasets:
        dataset.close()

manifest = {
    "source": source_url,
    "sources": [str(path) for path in sources],
    "output": str(output),
    "created_at": datetime.now(UTC).isoformat(),
}
(output.parent / "dem_manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True),
    encoding="utf-8",
)

with rasterio.open(output) as merged:
    print(f"wrote {output}")
    print(f"size {merged.width}x{merged.height}, bands={merged.count}, crs={merged.crs}")
    print(f"bounds {tuple(round(value, 3) for value in merged.bounds)}")
PY
