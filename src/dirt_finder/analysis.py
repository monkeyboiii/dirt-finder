from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import geometry_mask, shapes
from rasterio.mask import mask as raster_mask
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from dirt_finder.config import AppConfig, FilterConfig, ScoringWeights


METRIC_COLUMNS = [
    "rank",
    "area_m2",
    "mean_slope_deg",
    "max_slope_deg",
    "nearest_road_m",
    "allowed_landcover_fraction",
    "nearby_slope_score",
    "score",
]


def analyze_sites(config: AppConfig) -> gpd.GeoDataFrame:
    _require_inputs(config)
    output_dir = config.resolved_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    analysis_crs = config.search.analysis_crs
    search_area = gpd.read_file(config.search_area_file()).to_crs(analysis_crs)
    search_area = search_area[["geometry"]].dissolve().reset_index(drop=True)

    analysis_cache = config.paths.cache_dir / "analysis"
    analysis_cache.mkdir(parents=True, exist_ok=True)
    dem_path = _ensure_projected_raster(
        config.dem_file(),
        analysis_cache / "dem_projected.tif",
        analysis_crs,
        Resampling.bilinear,
    )

    dem, transform, crs = _read_masked_dem(dem_path, search_area)
    slope = calculate_slope(dem, transform)
    flat_mask = np.isfinite(slope) & (slope <= config.filters.flat_slope_degrees)
    flat_polygons = polygonize_flat_areas(flat_mask, transform, crs)
    candidates = _clip_and_filter_flat_polygons(flat_polygons, search_area, config.filters.min_area_m2)

    if candidates.empty:
        result = _empty_candidates(analysis_crs)
        _write_outputs(config, result)
        return result

    roads = gpd.read_file(config.roads_file()).to_crs(analysis_crs)
    metrics = _measure_candidates(candidates, roads, slope, transform, config)
    scored = score_candidates(metrics, config.filters, config.weights)
    filtered = scored[
        (scored["nearest_road_m"] <= config.filters.max_road_distance_m)
        & (scored["allowed_landcover_fraction"] >= config.filters.min_allowed_landcover_fraction)
    ].copy()
    filtered = filtered.sort_values("score", ascending=False).reset_index(drop=True)
    filtered.insert(0, "rank", range(1, len(filtered) + 1))

    _write_outputs(config, filtered)
    return filtered


def calculate_slope(dem: np.ndarray, transform: object) -> np.ndarray:
    """Calculate slope in degrees from a projected DEM array."""
    x_resolution = abs(float(transform.a))
    y_resolution = abs(float(transform.e))
    dem_float = dem.astype("float64", copy=False)
    gradient_y, gradient_x = np.gradient(dem_float, y_resolution, x_resolution)
    rise_run = np.sqrt(np.square(gradient_x) + np.square(gradient_y))
    slope = np.degrees(np.arctan(rise_run))
    slope[~np.isfinite(dem_float)] = np.nan
    return slope


def polygonize_flat_areas(flat_mask: np.ndarray, transform: object, crs: object) -> gpd.GeoDataFrame:
    if not np.any(flat_mask):
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=crs)
    geometries = [
        shape(geometry)
        for geometry, value in shapes(
            flat_mask.astype("uint8"),
            mask=flat_mask,
            transform=transform,
        )
        if value == 1
    ]
    return gpd.GeoDataFrame(geometry=geometries, crs=crs)


def score_candidates(
    candidates: gpd.GeoDataFrame,
    filters: FilterConfig,
    weights: ScoringWeights,
) -> gpd.GeoDataFrame:
    scored = candidates.copy()
    area_score = np.minimum(scored["area_m2"] / filters.target_area_m2, 1.0)
    flatness_score = 1.0 - np.minimum(scored["mean_slope_deg"] / filters.flat_slope_degrees, 1.0)
    road_score = 1.0 - np.minimum(scored["nearest_road_m"] / max(filters.max_road_distance_m, 1), 1.0)
    vegetation_score = scored["allowed_landcover_fraction"].clip(0, 1)
    nearby_slope_score = scored["nearby_slope_score"].clip(0, 1)

    weighted = (
        area_score * weights.area
        + flatness_score * weights.flatness
        + road_score * weights.road_access
        + vegetation_score * weights.vegetation
        + nearby_slope_score * weights.nearby_slope
    )
    scored["score"] = (weighted / weights.total * 100.0).round(2)
    return scored


def landcover_allowed_fraction(
    geometry: object,
    geometry_crs: object,
    landcover_path: Path,
    allowed_classes: list[int],
) -> float:
    import rasterio

    with rasterio.open(landcover_path) as source:
        geometry_in_lc_crs = (
            gpd.GeoSeries([geometry], crs=geometry_crs).to_crs(source.crs).iloc[0]
            if source.crs != geometry_crs
            else geometry
        )
        try:
            data, _ = raster_mask(source, [mapping(geometry_in_lc_crs)], crop=True, filled=False)
        except ValueError:
            return 0.0

    band = data[0]
    values = band.compressed() if np.ma.isMaskedArray(band) else band[np.isfinite(band)]
    values = values[values != 0]
    if values.size == 0:
        return 0.0
    return float(np.isin(values, allowed_classes).sum() / values.size)


def _require_inputs(config: AppConfig) -> None:
    required = [
        config.search_area_file(),
        config.roads_file(),
        config.dem_file(),
        config.landcover_file(),
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing analysis inputs. Run `dirt-finder fetch` first or set manual paths. "
            f"Missing: {', '.join(missing)}"
        )


def _ensure_projected_raster(
    source_path: Path,
    output_path: Path,
    destination_crs: str,
    resampling: Resampling,
) -> Path:
    import rasterio

    with rasterio.open(source_path) as source:
        if str(source.crs) == destination_crs:
            return source_path
        if output_path.exists():
            return output_path
        transform, width, height = calculate_default_transform(
            source.crs,
            destination_crs,
            source.width,
            source.height,
            *source.bounds,
        )
        profile = source.profile.copy()
        profile.update(
            crs=destination_crs,
            transform=transform,
            width=width,
            height=height,
            compress="deflate",
            tiled=True,
            bigtiff="if_safer",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_path, "w", **profile) as destination:
            for index in range(1, source.count + 1):
                reproject(
                    source=rasterio.band(source, index),
                    destination=rasterio.band(destination, index),
                    src_transform=source.transform,
                    src_crs=source.crs,
                    dst_transform=transform,
                    dst_crs=destination_crs,
                    resampling=resampling,
                )
    return output_path


def _read_masked_dem(path: Path, search_area: gpd.GeoDataFrame) -> tuple[np.ndarray, object, object]:
    import rasterio

    with rasterio.open(path) as source:
        geometries = [mapping(geometry) for geometry in search_area.to_crs(source.crs).geometry]
        data, transform = raster_mask(source, geometries, crop=True, filled=False)
        crs = source.crs
    band = data[0]
    dem = band.filled(np.nan) if np.ma.isMaskedArray(band) else band.astype(float)
    return dem, transform, crs


def _clip_and_filter_flat_polygons(
    flat_polygons: gpd.GeoDataFrame,
    search_area: gpd.GeoDataFrame,
    min_area_m2: float,
) -> gpd.GeoDataFrame:
    if flat_polygons.empty:
        return flat_polygons
    clipped = gpd.overlay(
        flat_polygons[["geometry"]],
        search_area[["geometry"]],
        how="intersection",
        keep_geom_type=False,
    )
    if clipped.empty:
        return clipped
    clipped = clipped.explode(index_parts=False).reset_index(drop=True)
    clipped["geometry"] = clipped.geometry.make_valid()
    clipped["area_m2"] = clipped.geometry.area
    return clipped[clipped["area_m2"] >= min_area_m2].copy()


def _measure_candidates(
    candidates: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    slope: np.ndarray,
    transform: object,
    config: AppConfig,
) -> gpd.GeoDataFrame:
    measured = candidates.copy()
    road_union = unary_union(list(roads.geometry)) if not roads.empty else None

    rows = []
    for geometry in measured.geometry:
        slope_values = _raster_values_for_geometry(slope, transform, geometry)
        mean_slope = float(np.nanmean(slope_values)) if slope_values.size else float("nan")
        max_slope = float(np.nanmax(slope_values)) if slope_values.size else float("nan")
        nearest_road = float(geometry.distance(road_union)) if road_union is not None else float("inf")
        allowed_fraction = landcover_allowed_fraction(
            geometry,
            measured.crs,
            config.landcover_file(),
            config.filters.allowed_landcover_classes,
        )
        nearby_score = _nearby_slope_score(geometry, slope, transform, config)
        rows.append(
            {
                "mean_slope_deg": mean_slope,
                "max_slope_deg": max_slope,
                "nearest_road_m": nearest_road,
                "allowed_landcover_fraction": allowed_fraction,
                "nearby_slope_score": nearby_score,
            }
        )

    combined = pd.concat([measured.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    return gpd.GeoDataFrame(combined, geometry="geometry", crs=measured.crs)


def _raster_values_for_geometry(
    raster: np.ndarray,
    transform: object,
    geometry: object,
) -> np.ndarray:
    geom_mask = geometry_mask(
        [mapping(geometry)],
        out_shape=raster.shape,
        transform=transform,
        invert=True,
    )
    values = raster[geom_mask]
    return values[np.isfinite(values)]


def _nearby_slope_score(
    geometry: object,
    slope: np.ndarray,
    transform: object,
    config: AppConfig,
) -> float:
    buffer_distance = config.filters.nearby_slope_buffer_m
    if buffer_distance <= 0:
        return 0.0
    nearby_geometry = geometry.buffer(buffer_distance).difference(geometry)
    if nearby_geometry.is_empty:
        return 0.0
    values = _raster_values_for_geometry(slope, transform, nearby_geometry)
    if values.size == 0:
        return 0.0
    interesting = values[values > config.filters.flat_slope_degrees]
    if interesting.size == 0:
        return 0.0
    mean_extra_slope = float(np.nanmean(interesting) - config.filters.flat_slope_degrees)
    return max(0.0, min(mean_extra_slope / 20.0, 1.0))


def _write_outputs(config: AppConfig, candidates: gpd.GeoDataFrame) -> None:
    geojson_path = config.candidates_geojson_file()
    csv_path = config.candidates_csv_file()
    metadata_path = config.metadata_file()

    if candidates.empty:
        geojson_path.write_text('{"type":"FeatureCollection","features":[]}\n', encoding="utf-8")
        pd.DataFrame(columns=METRIC_COLUMNS).to_csv(csv_path, index=False)
    else:
        centroids = gpd.GeoSeries(candidates.geometry.centroid, crs=candidates.crs).to_crs("EPSG:4326")
        output_gdf = candidates.to_crs("EPSG:4326")
        output_gdf.to_file(geojson_path, driver="GeoJSON")
        csv = output_gdf.copy()
        csv["centroid_lon"] = centroids.x
        csv["centroid_lat"] = centroids.y
        csv.drop(columns="geometry").to_csv(csv_path, index=False)

    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate_count": int(len(candidates)),
        "config": config.model_dump(mode="json", exclude_none=True),
        "inputs": {
            "search_area": str(config.search_area_file()),
            "roads": str(config.roads_file()),
            "dem": str(config.dem_file()),
            "landcover": str(config.landcover_file()),
        },
        "outputs": {
            "geojson": str(geojson_path),
            "csv": str(csv_path),
            "map": str(config.map_file()),
        },
        "disclaimer": (
            "Reconnaissance candidates only; results do not indicate ownership, legal access, "
            "permits, environmental clearance, or engineering suitability."
        ),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def _empty_candidates(crs: object) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns=METRIC_COLUMNS + ["geometry"], geometry="geometry", crs=crs)
