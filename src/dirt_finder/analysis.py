from __future__ import annotations

import atexit
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.errors import WindowError
from rasterio.features import geometry_mask, shapes
from rasterio.mask import mask as raster_mask
from rasterio.windows import Window, transform as window_transform
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rich.console import Console
from shapely.geometry import mapping, shape

from dirt_finder.config import AppConfig, FilterConfig, ScoringWeights


console = Console()

_WORKER_SLOPE: np.ndarray | None = None
_WORKER_TRANSFORM: object | None = None
_WORKER_LANDCOVER_SOURCE: object | None = None
_WORKER_ALLOWED_CLASSES: list[int] | None = None
_WORKER_FLAT_SLOPE_DEGREES: float | None = None
_WORKER_NEARBY_SLOPE_BUFFER_M: float | None = None

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

    console.print("[cyan]Analyzing search area[/cyan]")
    analysis_crs = config.search.analysis_crs
    search_area = gpd.read_file(config.search_area_file()).to_crs(analysis_crs)
    search_area = search_area[["geometry"]].dissolve().reset_index(drop=True)

    analysis_cache = config.paths.cache_dir / "analysis"
    analysis_cache.mkdir(parents=True, exist_ok=True)
    console.print("[cyan]Preparing DEM[/cyan]")
    dem_path = _ensure_projected_raster(
        config.dem_file(),
        analysis_cache / "dem_projected.tif",
        analysis_crs,
        Resampling.bilinear,
    )

    console.print(f"[cyan]Reading DEM[/cyan] {dem_path}")
    dem, transform, crs = _read_masked_dem(dem_path, search_area)
    console.print(f"[cyan]Calculating slope[/cyan] {dem.shape[1]}x{dem.shape[0]} cells")
    slope = calculate_slope(dem, transform)
    flat_mask = np.isfinite(slope) & (slope <= config.filters.flat_slope_degrees)
    console.print("[cyan]Polygonizing flat areas[/cyan]")
    flat_polygons = polygonize_flat_areas(flat_mask, transform, crs)
    console.print(f"[cyan]Filtering flat polygons[/cyan] {len(flat_polygons)} raw polygons")
    candidates = _clip_and_filter_flat_polygons(flat_polygons, search_area, config.filters.min_area_m2)
    console.print(f"[cyan]Candidate polygons[/cyan] {len(candidates)} after area filter")

    if candidates.empty:
        result = _empty_candidates(analysis_crs)
        _write_outputs(config, result)
        return result

    console.print("[cyan]Reading roads[/cyan]")
    roads = gpd.read_file(config.roads_file()).to_crs(analysis_crs)
    worker_count = _measurement_worker_count(len(candidates))
    worker_label = "serial" if worker_count == 1 else f"{worker_count} processes"
    console.print(f"[cyan]Measuring candidates[/cyan] {len(candidates)} candidates, {worker_label}")
    metrics = _measure_candidates(candidates, roads, slope, transform, config)
    console.print("[cyan]Scoring candidates[/cyan]")
    scored = score_candidates(metrics, config.filters, config.weights)
    filtered = scored[
        (scored["nearest_road_m"] <= config.filters.max_road_distance_m)
        & (scored["allowed_landcover_fraction"] >= config.filters.min_allowed_landcover_fraction)
    ].copy()
    filtered = filtered.sort_values("score", ascending=False).reset_index(drop=True)
    filtered.insert(0, "rank", range(1, len(filtered) + 1))

    _write_outputs(config, filtered)
    console.print(f"[green]Analysis outputs written[/green] {len(filtered)} candidates")
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
    dem = band.astype("float64").filled(np.nan) if np.ma.isMaskedArray(band) else band.astype(float)
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
    measure_started = time.perf_counter()

    nearest_started = time.perf_counter()
    nearest_roads = _nearest_road_distances(measured, roads)
    _log_elapsed("Nearest-road distances", nearest_started)

    import rasterio

    landcover_path = config.landcover_file()
    with rasterio.open(landcover_path) as landcover:
        landcover_crs = landcover.crs
    simplify_started = time.perf_counter()
    measurement_tolerance = max(abs(float(transform.a)), abs(float(transform.e)))
    measurement_geometries = _simplify_geometry_list(measured.geometry, measurement_tolerance)
    landcover_series = gpd.GeoSeries(measured.geometry, crs=measured.crs)
    landcover_geometries = (
        landcover_series.to_crs(landcover_crs).tolist()
        if landcover_crs != measured.crs
        else measured.geometry.tolist()
    )
    _log_elapsed(f"Prepared measurement geometries at {measurement_tolerance:.1f}m tolerance", simplify_started)

    rows = []
    total = len(measured)
    worker_count = _measurement_worker_count(total)
    log_every = max(1, min(100, total // 10 or 1))
    if worker_count == 1:
        serial_started = time.perf_counter()
        for index, (geometry, landcover_geometry) in enumerate(
            zip(measurement_geometries, landcover_geometries, strict=True),
            start=1,
        ):
            row = _measure_candidate(
                geometry,
                landcover_geometry,
                slope,
                transform,
                landcover_path,
                config.filters.allowed_landcover_classes,
                config.filters.flat_slope_degrees,
                config.filters.nearby_slope_buffer_m,
            )
            row["nearest_road_m"] = float(nearest_roads[index - 1])
            rows.append(row)
            if total >= 10 and (index == total or index % log_every == 0):
                console.print(f"[dim]Measured {index}/{total} candidates[/dim]")
        _log_elapsed("Measured candidates serially", serial_started)
    else:
        rows = [None] * total
        task_order = sorted(range(total), key=lambda index: measured.geometry.iloc[index].area, reverse=True)
        tasks = [
            (index, measurement_geometries[index], landcover_geometries[index])
            for index in task_order
        ]
        slope_worker_path = config.paths.cache_dir / "analysis" / "slope_worker.npy"
        slope_worker_path.parent.mkdir(parents=True, exist_ok=True)
        console.print(f"[dim]Writing worker slope cache {slope_worker_path}[/dim]")
        slope_cache_started = time.perf_counter()
        np.save(slope_worker_path, slope, allow_pickle=False)
        _log_elapsed("Wrote worker slope cache", slope_cache_started)

        pool_started = time.perf_counter()
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_measure_worker,
            initargs=(
                str(slope_worker_path),
                transform,
                str(landcover_path),
                config.filters.allowed_landcover_classes,
                config.filters.flat_slope_degrees,
                config.filters.nearby_slope_buffer_m,
            ),
        ) as executor:
            submit_started = time.perf_counter()
            futures = [executor.submit(_measure_candidate_worker, task) for task in tasks]
            _log_elapsed(f"Submitted {len(futures)} measurement tasks", submit_started)
            for completed, future in enumerate(as_completed(futures), start=1):
                index, row = future.result()
                row["nearest_road_m"] = float(nearest_roads[index])
                rows[index] = row
                if total >= 10 and (completed == total or completed % log_every == 0):
                    console.print(f"[dim]Measured {completed}/{total} candidates[/dim]")
        _log_elapsed(f"Measured candidates with {worker_count} processes", pool_started)

    attach_started = time.perf_counter()
    result = _attach_measurement_rows(measured, rows)
    _log_elapsed("Attached measurement columns", attach_started)
    _log_elapsed("Candidate measurement total", measure_started)
    return result


def _log_elapsed(label: str, started: float) -> None:
    console.print(f"[dim]{label} in {time.perf_counter() - started:.2f}s[/dim]")


def _attach_measurement_rows(
    measured: gpd.GeoDataFrame,
    rows: list[dict[str, float] | None],
) -> gpd.GeoDataFrame:
    if any(row is None for row in rows):
        missing = sum(row is None for row in rows)
        raise RuntimeError(f"Measurement finished with {missing} missing candidate rows.")

    result = measured.reset_index(drop=True).copy()
    metric_names = [
        "mean_slope_deg",
        "max_slope_deg",
        "nearest_road_m",
        "allowed_landcover_fraction",
        "nearby_slope_score",
    ]
    for name in metric_names:
        result[name] = [row[name] for row in rows if row is not None]
    return gpd.GeoDataFrame(result, geometry="geometry", crs=measured.crs)


def _simplify_geometry_list(geometries: gpd.GeoSeries, tolerance: float) -> list[object]:
    if tolerance <= 0:
        return geometries.tolist()

    simplified = geometries.simplify(tolerance, preserve_topology=True)
    return [
        simplified_geometry if not simplified_geometry.is_empty else geometry
        for geometry, simplified_geometry in zip(geometries, simplified, strict=True)
    ]


def _nearest_road_distances(candidates: gpd.GeoDataFrame, roads: gpd.GeoDataFrame) -> np.ndarray:
    if roads.empty:
        return np.full(len(candidates), np.inf)

    left = gpd.GeoDataFrame(
        {"candidate_index": np.arange(len(candidates))},
        geometry=candidates.geometry,
        crs=candidates.crs,
    )
    nearest = gpd.sjoin_nearest(
        left,
        roads[["geometry"]].reset_index(drop=True),
        how="left",
        distance_col="nearest_road_m",
    )
    distances = nearest.groupby("candidate_index")["nearest_road_m"].min()
    return distances.reindex(np.arange(len(candidates)), fill_value=np.inf).to_numpy(dtype=float)


def _measurement_worker_count(total: int) -> int:
    if total < 200:
        return 1

    configured = os.environ.get("DIRT_FINDER_MEASURE_WORKERS")
    if configured:
        try:
            return max(1, int(configured))
        except ValueError as exc:
            raise ValueError("DIRT_FINDER_MEASURE_WORKERS must be an integer") from exc

    cpu_count = os.cpu_count() or 2
    return max(1, min(3, cpu_count // 2))


def _init_measure_worker(
    slope_path: str,
    transform: object,
    landcover_path: str,
    allowed_classes: list[int],
    flat_slope_degrees: float,
    nearby_slope_buffer_m: float,
) -> None:
    import rasterio

    global _WORKER_ALLOWED_CLASSES
    global _WORKER_FLAT_SLOPE_DEGREES
    global _WORKER_LANDCOVER_SOURCE
    global _WORKER_NEARBY_SLOPE_BUFFER_M
    global _WORKER_SLOPE
    global _WORKER_TRANSFORM

    _WORKER_SLOPE = np.load(slope_path, mmap_mode="r")
    _WORKER_TRANSFORM = transform
    _WORKER_LANDCOVER_SOURCE = rasterio.open(landcover_path)
    _WORKER_ALLOWED_CLASSES = allowed_classes
    _WORKER_FLAT_SLOPE_DEGREES = flat_slope_degrees
    _WORKER_NEARBY_SLOPE_BUFFER_M = nearby_slope_buffer_m
    atexit.register(_close_worker_sources)


def _close_worker_sources() -> None:
    if _WORKER_LANDCOVER_SOURCE is not None:
        _WORKER_LANDCOVER_SOURCE.close()


def _measure_candidate_worker(task: tuple[int, object, object]) -> tuple[int, dict[str, float]]:
    index, geometry, landcover_geometry = task
    if (
        _WORKER_SLOPE is None
        or _WORKER_TRANSFORM is None
        or _WORKER_LANDCOVER_SOURCE is None
        or _WORKER_ALLOWED_CLASSES is None
        or _WORKER_FLAT_SLOPE_DEGREES is None
        or _WORKER_NEARBY_SLOPE_BUFFER_M is None
    ):
        raise RuntimeError("Measurement worker was not initialized.")

    row = _measure_candidate_with_landcover_source(
        geometry,
        landcover_geometry,
        _WORKER_SLOPE,
        _WORKER_TRANSFORM,
        _WORKER_LANDCOVER_SOURCE,
        _WORKER_ALLOWED_CLASSES,
        _WORKER_FLAT_SLOPE_DEGREES,
        _WORKER_NEARBY_SLOPE_BUFFER_M,
    )
    return index, row


def _measure_candidate(
    geometry: object,
    landcover_geometry: object,
    slope: np.ndarray,
    transform: object,
    landcover_path: Path,
    allowed_classes: list[int],
    flat_slope_degrees: float,
    nearby_slope_buffer_m: float,
) -> dict[str, float]:
    import rasterio

    with rasterio.open(landcover_path) as landcover_source:
        return _measure_candidate_with_landcover_source(
            geometry,
            landcover_geometry,
            slope,
            transform,
            landcover_source,
            allowed_classes,
            flat_slope_degrees,
            nearby_slope_buffer_m,
        )


def _measure_candidate_with_landcover_source(
    geometry: object,
    landcover_geometry: object,
    slope: np.ndarray,
    transform: object,
    landcover_source: object,
    allowed_classes: list[int],
    flat_slope_degrees: float,
    nearby_slope_buffer_m: float,
) -> dict[str, float]:
    slope_values = _raster_values_for_geometry(slope, transform, geometry)
    mean_slope = float(np.nanmean(slope_values)) if slope_values.size else float("nan")
    max_slope = float(np.nanmax(slope_values)) if slope_values.size else float("nan")
    allowed_fraction = _landcover_allowed_fraction_in_source(
        landcover_source,
        landcover_geometry,
        allowed_classes,
    )
    nearby_score = _nearby_slope_score_from_params(
        geometry,
        slope,
        transform,
        flat_slope_degrees,
        nearby_slope_buffer_m,
    )
    return {
        "mean_slope_deg": mean_slope,
        "max_slope_deg": max_slope,
        "allowed_landcover_fraction": allowed_fraction,
        "nearby_slope_score": nearby_score,
    }


def _raster_values_for_geometry(
    raster: np.ndarray,
    transform: object,
    geometry: object,
) -> np.ndarray:
    window = _geometry_window(raster.shape, transform, geometry)
    if window is None:
        return np.array([], dtype=raster.dtype)

    row_start = int(window.row_off)
    row_stop = row_start + int(window.height)
    col_start = int(window.col_off)
    col_stop = col_start + int(window.width)
    raster_window = raster[row_start:row_stop, col_start:col_stop]
    if raster_window.size == 0:
        return np.array([], dtype=raster.dtype)

    geom_mask = geometry_mask(
        [mapping(geometry)],
        out_shape=raster_window.shape,
        transform=window_transform(window, transform),
        invert=True,
    )
    values = raster_window[geom_mask]
    return values[np.isfinite(values)]


def _geometry_window(
    raster_shape: tuple[int, int],
    transform: object,
    geometry: object,
) -> Window | None:
    import rasterio.windows

    if geometry.is_empty:
        return None

    height, width = raster_shape
    try:
        window = rasterio.windows.from_bounds(*geometry.bounds, transform=transform)
    except WindowError:
        return None

    col_start = max(0, math.floor(window.col_off))
    row_start = max(0, math.floor(window.row_off))
    col_stop = min(width, math.ceil(window.col_off + window.width))
    row_stop = min(height, math.ceil(window.row_off + window.height))
    if col_stop <= col_start or row_stop <= row_start:
        return None
    return Window(col_start, row_start, col_stop - col_start, row_stop - row_start)


def _landcover_allowed_fraction_in_source(
    source: object,
    geometry: object,
    allowed_classes: list[int],
) -> float:
    try:
        data, _ = raster_mask(source, [mapping(geometry)], crop=True, filled=False)
    except ValueError:
        return 0.0

    band = data[0]
    values = band.compressed() if np.ma.isMaskedArray(band) else band[np.isfinite(band)]
    values = values[values != 0]
    if values.size == 0:
        return 0.0
    return float(np.isin(values, allowed_classes).sum() / values.size)


def _nearby_slope_score_from_params(
    geometry: object,
    slope: np.ndarray,
    transform: object,
    flat_slope_degrees: float,
    nearby_slope_buffer_m: float,
) -> float:
    buffer_distance = nearby_slope_buffer_m
    if buffer_distance <= 0:
        return 0.0
    nearby_geometry = geometry.buffer(buffer_distance).difference(geometry)
    if nearby_geometry.is_empty:
        return 0.0
    values = _raster_values_for_geometry(slope, transform, nearby_geometry)
    if values.size == 0:
        return 0.0
    interesting = values[values > flat_slope_degrees]
    if interesting.size == 0:
        return 0.0
    mean_extra_slope = float(np.nanmean(interesting) - flat_slope_degrees)
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
