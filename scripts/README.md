# Scripts

## Download GSCloud DEM Tiles

Reference: https://developer.aliyun.com/article/1231915

The GSCloud workflow for ASTER GDEM is:

1. Log in to https://www.gscloud.cn/home.
2. Open the ASTER GDEM 30M resolution digital elevation product.
3. Search by area, longitude/latitude range, or tile/data identifier.
4. Select the needed tiles. For the Hangzhou run, the four tiles are:
   - `ASTGTM_N30E120`
   - `ASTGTM_N30E119`
   - `ASTGTM_N29E120`
   - `ASTGTM_N29E119`
5. Use the FTP address, account, and password shown by GSCloud to download the
   `.img.zip` archives.
6. Merge the extracted `.img` rasters into `data/cache/dem/dem_mosaic.tif`.

This repo keeps that process in `download_merge_gscloud_dem.sh`.

Create a local `.env.gscloud` file in the repo root:

```bash
GSCLOUD_FTP_USER="..."
GSCLOUD_FTP_PASS="..."
```

Then run:

```bash
scripts/download_merge_gscloud_dem.sh
```

The script downloads the four archives, validates them with `unzip`, extracts
the `.img` files, and merges them with `rasterio` through `uv run`. The GSCloud
FTP server reports incorrect file sizes for these archives, so the script uses
`curl --ignore-content-length`.
