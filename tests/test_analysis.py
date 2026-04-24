from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from dirt_finder.analysis import (
    _raster_values_for_geometry,
    analyze_sites,
    calculate_slope,
    landcover_allowed_fraction,
    polygonize_flat_areas,
    score_candidates,
)
from dirt_finder.config import AppConfig, FilterConfig, PathConfig, ScoringWeights, SearchConfig


CRS = "EPSG:32651"


def test_calculate_slope_for_flat_dem() -> None:
    dem = np.zeros((5, 5), dtype="float32")
    transform = from_origin(0, 50, 10, 10)

    slope = calculate_slope(dem, transform)

    assert np.nanmax(slope) == pytest.approx(0)


def test_polygonize_flat_areas_uses_cell_area() -> None:
    flat = np.zeros((5, 5), dtype=bool)
    flat[1:4, 1:4] = True
    transform = from_origin(0, 50, 10, 10)

    polygons = polygonize_flat_areas(flat, transform, CRS)

    assert len(polygons) == 1
    assert polygons.geometry.iloc[0].area == pytest.approx(900)


def test_score_candidates_prefers_bigger_flatter_closer_sites() -> None:
    candidates = gpd.GeoDataFrame(
        {
            "area_m2": [20_000, 5_000],
            "mean_slope_deg": [1.0, 4.5],
            "max_slope_deg": [2.0, 5.0],
            "nearest_road_m": [50.0, 450.0],
            "allowed_landcover_fraction": [1.0, 0.6],
            "nearby_slope_score": [0.7, 0.0],
        },
        geometry=[box(0, 0, 100, 100), box(200, 0, 250, 100)],
        crs=CRS,
    )

    scored = score_candidates(candidates, FilterConfig(), ScoringWeights())

    assert scored.loc[0, "score"] > scored.loc[1, "score"]
    assert scored["score"].between(0, 100).all()


def test_landcover_allowed_fraction(tmp_path: Path) -> None:
    landcover = tmp_path / "landcover.tif"
    transform = from_origin(0, 40, 10, 10)
    data = np.array(
        [
            [30, 30, 10, 10],
            [30, 30, 10, 10],
            [60, 60, 50, 50],
            [60, 60, 50, 50],
        ],
        dtype="uint8",
    )
    _write_raster(landcover, data, transform, CRS, nodata=0)

    fraction = landcover_allowed_fraction(box(0, 0, 40, 40), CRS, landcover, [30, 60])

    assert fraction == pytest.approx(0.5)


def test_raster_values_for_geometry_uses_geometry_window() -> None:
    raster = np.arange(100, dtype="float32").reshape((10, 10))
    transform = from_origin(0, 10, 1, 1)

    values = _raster_values_for_geometry(raster, transform, box(2, 4, 5, 7))

    assert sorted(values.tolist()) == [32.0, 33.0, 34.0, 42.0, 43.0, 44.0, 52.0, 53.0, 54.0]


def test_analyze_sites_end_to_end_with_manual_inputs(tmp_path: Path) -> None:
    search_area_path = tmp_path / "search_area.geojson"
    roads_path = tmp_path / "roads.geojson"
    dem_path = tmp_path / "dem.tif"
    landcover_path = tmp_path / "landcover.tif"

    search_area = gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=CRS)
    search_area.to_file(search_area_path, driver="GeoJSON")
    roads = gpd.GeoDataFrame(geometry=[LineString([(0, -10), (0, 110)])], crs=CRS)
    roads.to_file(roads_path, driver="GeoJSON")

    transform = from_origin(0, 100, 10, 10)
    _write_raster(dem_path, np.zeros((10, 10), dtype="float32"), transform, CRS, nodata=-9999)
    _write_raster(landcover_path, np.full((10, 10), 30, dtype="uint8"), transform, CRS, nodata=0)

    config = AppConfig(
        search=SearchConfig(analysis_crs=CRS),
        filters=FilterConfig(min_area_m2=1_000, min_allowed_landcover_fraction=0.5),
        paths=PathConfig(
            cache_dir=tmp_path / "cache",
            output_dir=tmp_path / "outputs",
            dem_path=dem_path,
            landcover_path=landcover_path,
            roads_path=roads_path,
            search_area_path=search_area_path,
        ),
    )

    candidates = analyze_sites(config)

    assert len(candidates) == 1
    assert candidates.iloc[0]["area_m2"] == pytest.approx(10_000)
    assert config.candidates_geojson_file().exists()
    assert config.candidates_csv_file().exists()
    assert config.metadata_file().exists()


def _write_raster(
    path: Path,
    data: np.ndarray,
    transform: rasterio.Affine,
    crs: str,
    nodata: int | float,
) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as destination:
        destination.write(data, 1)
