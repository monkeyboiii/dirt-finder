from __future__ import annotations

import json
import math
import os
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import networkx as nx
from rich.console import Console
from shapely.geometry import box
from shapely.ops import unary_union

from dirt_finder.config import AppConfig

console = Console()


def fetch_data(config: AppConfig) -> None:
    """Fetch or validate all input datasets needed by the analysis stage."""
    config.resolved_cache_dir().mkdir(parents=True, exist_ok=True)
    ensure_osm_inputs(config)
    ensure_dem(config)
    ensure_landcover(config)


def ensure_osm_inputs(config: AppConfig) -> None:
    import osmnx as ox

    osm_cache = config.paths.cache_dir / "osmnx_http"
    osm_cache.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(osm_cache)

    boundary_path = config.boundary_file()
    roads_path = config.roads_file()
    graph_path = config.graph_file()
    isochrone_path = config.isochrone_file()
    search_area_path = config.search_area_file()

    boundary_path.parent.mkdir(parents=True, exist_ok=True)

    if not boundary_path.exists():
        console.print(f"[cyan]Fetching OSM boundary[/cyan] {config.search.place_name}")
        boundary = ox.geocode_to_gdf(config.search.place_name)
        boundary.to_file(boundary_path, driver="GeoJSON")
    else:
        console.print(f"[dim]Using cached boundary[/dim] {boundary_path}")

    if graph_path.exists():
        console.print(f"[dim]Using cached drive graph[/dim] {graph_path}")
        graph = ox.load_graphml(graph_path)
    else:
        console.print("[cyan]Fetching OSM drive graph[/cyan]")
        graph = ox.graph_from_point(
            (config.search.center_lat, config.search.center_lon),
            dist=config.search.graph_fetch_radius_m,
            network_type="drive",
            simplify=True,
        )
        graph = _add_speed_and_travel_time(graph, ox)
        ox.save_graphml(graph, graph_path)

    graph = _ensure_edge_travel_times(graph, ox)

    if not roads_path.exists():
        console.print(f"[cyan]Writing road layer[/cyan] {roads_path}")
        _, roads = ox.graph_to_gdfs(graph)
        roads = _clean_roads_for_geojson(roads.reset_index())
        roads.to_file(roads_path, driver="GeoJSON")
    else:
        console.print(f"[dim]Using cached roads[/dim] {roads_path}")

    if not isochrone_path.exists():
        console.print("[cyan]Computing drive-time isochrone[/cyan]")
        isochrone = build_drive_time_isochrone(config, graph, ox)
        isochrone.to_file(isochrone_path, driver="GeoJSON")
    else:
        console.print(f"[dim]Using cached isochrone[/dim] {isochrone_path}")

    if not search_area_path.exists():
        console.print(f"[cyan]Writing search area[/cyan] {search_area_path}")
        boundary = gpd.read_file(boundary_path).to_crs("EPSG:4326")
        isochrone = gpd.read_file(isochrone_path).to_crs("EPSG:4326")
        search_area = gpd.overlay(
            boundary[["geometry"]],
            isochrone[["geometry"]],
            how="intersection",
            keep_geom_type=False,
        )
        if search_area.empty:
            search_area = isochrone[["geometry"]]
        search_area = search_area.dissolve().reset_index(drop=True)
        search_area.to_file(search_area_path, driver="GeoJSON")
    else:
        console.print(f"[dim]Using cached search area[/dim] {search_area_path}")


def build_drive_time_isochrone(config: AppConfig, graph: object, ox: object) -> gpd.GeoDataFrame:
    center_node = ox.distance.nearest_nodes(
        graph,
        config.search.center_lon,
        config.search.center_lat,
    )
    cutoff_seconds = config.search.drive_time_minutes * 60
    travel_times = nx.single_source_dijkstra_path_length(
        graph,
        center_node,
        cutoff=cutoff_seconds,
        weight="travel_time",
    )
    if not travel_times:
        raise RuntimeError("No reachable OSM nodes found for the configured drive-time area.")

    nodes = ox.graph_to_gdfs(graph, edges=False)
    reachable = nodes.loc[list(travel_times.keys())]
    reachable_proj = reachable.to_crs(config.search.analysis_crs)
    buffered = reachable_proj.geometry.buffer(config.search.isochrone_node_buffer_m)
    polygon = unary_union(list(buffered))
    if polygon.is_empty:
        raise RuntimeError("Reachable OSM nodes produced an empty isochrone polygon.")

    return gpd.GeoDataFrame(geometry=[polygon], crs=config.search.analysis_crs).to_crs("EPSG:4326")


def ensure_dem(config: AppConfig) -> None:
    manual_path = config.paths.dem_path
    if manual_path is not None:
        if not manual_path.exists():
            raise FileNotFoundError(f"Configured DEM path does not exist: {manual_path}")
        console.print(f"[dim]Using configured DEM[/dim] {manual_path}")
        return

    output = config.dem_file()
    if output.exists():
        console.print(f"[dim]Using cached DEM[/dim] {output}")
        return

    username = os.environ.get("EARTHDATA_USERNAME")
    password = os.environ.get("EARTHDATA_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "Auto DEM download requires EARTHDATA_USERNAME and EARTHDATA_PASSWORD, "
            "or set paths.dem_path to a local DEM GeoTIFF in the config."
        )

    import earthaccess

    output.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = output.parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    bbox_wgs84 = _search_area_bounds(config)

    console.print("[cyan]Searching NASA Earthdata DEM granules[/cyan]")
    earthaccess.login(strategy="environment", persist=False)
    results = []
    for short_name in ("NASADEM_HGT", "SRTMGL1"):
        results = earthaccess.search_data(short_name=short_name, bounding_box=bbox_wgs84)
        if results:
            break
    if not results:
        raise RuntimeError("No NASADEM/SRTM granules found for the configured search area.")

    console.print(f"[cyan]Downloading {len(results)} DEM granules[/cyan]")
    downloaded = earthaccess.download(results, local_path=str(raw_dir))
    raster_sources = list(_raster_sources_from_paths([Path(path) for path in downloaded]))
    if not raster_sources:
        raster_sources = list(_raster_sources_from_paths(raw_dir.rglob("*")))
    if not raster_sources:
        raise RuntimeError("DEM download finished, but no readable raster files were found.")

    _mosaic_rasters(raster_sources, output)
    _write_manifest(output.parent / "dem_manifest.json", {"sources": [str(path) for path in raster_sources]})


def ensure_landcover(config: AppConfig) -> None:
    manual_path = config.paths.landcover_path
    if manual_path is not None:
        if not manual_path.exists():
            raise FileNotFoundError(f"Configured land-cover path does not exist: {manual_path}")
        console.print(f"[dim]Using configured land cover[/dim] {manual_path}")
        return

    output = config.landcover_file()
    if output.exists():
        console.print(f"[dim]Using cached land cover[/dim] {output}")
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = output.parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    bbox_wgs84 = _search_area_bounds(config)
    tile_ids = worldcover_tile_ids_for_bounds(bbox_wgs84)

    downloaded = []
    for tile_id in tile_ids:
        url = worldcover_url(tile_id)
        target = raw_dir / Path(url).name
        if not target.exists():
            console.print(f"[cyan]Downloading ESA WorldCover tile[/cyan] {tile_id}")
            try:
                urllib.request.urlretrieve(url, target)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "Unable to download ESA WorldCover automatically. "
                    "Set paths.landcover_path to a local WorldCover-style GeoTIFF "
                    f"or retry later. Failed URL: {url}"
                ) from exc
        downloaded.append(target)

    _mosaic_rasters([str(path) for path in downloaded], output, bounds=bbox_wgs84)
    _write_manifest(
        output.parent / "landcover_manifest.json",
        {"tiles": tile_ids, "sources": [str(path) for path in downloaded]},
    )


def worldcover_tile_ids_for_bounds(bounds: tuple[float, float, float, float]) -> list[str]:
    minx, miny, maxx, maxy = bounds
    lon_start = _floor_to_tile(minx)
    lon_stop = _floor_to_tile(maxx - 1e-9)
    lat_start = _floor_to_tile(miny)
    lat_stop = _floor_to_tile(maxy - 1e-9)

    tile_ids = []
    lon = lon_start
    while lon <= lon_stop:
        lat = lat_start
        while lat <= lat_stop:
            tile_ids.append(_format_worldcover_tile(lat, lon))
            lat += 3
        lon += 3
    return sorted(tile_ids)


def worldcover_url(tile_id: str) -> str:
    return (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
        f"ESA_WorldCover_10m_2021_v200_{tile_id}_Map.tif"
    )


def _floor_to_tile(value: float) -> int:
    return int(math.floor(value / 3.0) * 3)


def _format_worldcover_tile(lat: int, lon: int) -> str:
    lat_prefix = "N" if lat >= 0 else "S"
    lon_prefix = "E" if lon >= 0 else "W"
    return f"{lat_prefix}{abs(lat):02d}{lon_prefix}{abs(lon):03d}"


def _add_speed_and_travel_time(graph: object, ox: object) -> object:
    routing = getattr(ox, "routing", ox)
    graph = routing.add_edge_speeds(graph)
    graph = routing.add_edge_travel_times(graph)
    return graph


def _ensure_edge_travel_times(graph: object, ox: object) -> object:
    missing = False
    for _, _, _, data in graph.edges(keys=True, data=True):
        if "travel_time" not in data:
            missing = True
            break
        data["travel_time"] = float(data["travel_time"])
    if missing:
        graph = _add_speed_and_travel_time(graph, ox)
    return graph


def _clean_roads_for_geojson(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    keep_columns = [
        "u",
        "v",
        "key",
        "osmid",
        "name",
        "highway",
        "length",
        "speed_kph",
        "travel_time",
        "geometry",
    ]
    cleaned = roads[[column for column in keep_columns if column in roads.columns]].copy()
    for column in cleaned.columns:
        if column == "geometry":
            continue
        cleaned[column] = cleaned[column].map(_scalar_for_geojson)
    return cleaned


def _scalar_for_geojson(value: object) -> object:
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return value


def _search_area_bounds(config: AppConfig) -> tuple[float, float, float, float]:
    search_area_path = config.search_area_file()
    if not search_area_path.exists():
        center = config.search
        buffer_degrees = 1.0
        return (
            center.center_lon - buffer_degrees,
            center.center_lat - buffer_degrees,
            center.center_lon + buffer_degrees,
            center.center_lat + buffer_degrees,
        )
    search_area = gpd.read_file(search_area_path).to_crs("EPSG:4326")
    minx, miny, maxx, maxy = search_area.total_bounds
    return (float(minx), float(miny), float(maxx), float(maxy))


def _raster_sources_from_paths(paths: Iterable[Path]) -> Iterable[str]:
    for path in paths:
        suffix = path.suffix.lower()
        if suffix in {".tif", ".tiff", ".hgt"}:
            yield str(path)
        elif suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    lower = member.lower()
                    if lower.endswith((".tif", ".tiff", ".hgt")):
                        yield f"/vsizip/{path}/{member}"


def _mosaic_rasters(
    sources: list[str],
    output: Path,
    bounds: tuple[float, float, float, float] | None = None,
) -> None:
    import rasterio
    from rasterio.merge import merge

    output.parent.mkdir(parents=True, exist_ok=True)
    datasets = [rasterio.open(source) for source in sources]
    try:
        mosaic, transform = merge(datasets, bounds=bounds)
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
        )
        with rasterio.open(output, "w", **profile) as destination:
            destination.write(mosaic)
    finally:
        for dataset in datasets:
            dataset.close()


def _write_manifest(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def bounds_to_polygon(bounds: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Small public helper useful for smoke tests and ad hoc data prep."""
    return gpd.GeoDataFrame(geometry=[box(*bounds)], crs="EPSG:4326")
