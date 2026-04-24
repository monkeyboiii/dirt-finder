from __future__ import annotations

from pathlib import Path

import folium
import geopandas as gpd

from dirt_finder.config import AppConfig


def render_map(config: AppConfig) -> Path:
    config.resolved_output_dir().mkdir(parents=True, exist_ok=True)
    center = [config.search.center_lat, config.search.center_lon]
    fmap = folium.Map(location=center, zoom_start=10, tiles="OpenStreetMap", control_scale=True)

    if config.search_area_file().exists():
        boundary = gpd.read_file(config.search_area_file()).to_crs("EPSG:4326")
        folium.GeoJson(
            boundary,
            name="Search area",
            style_function=lambda _feature: {
                "color": "#1f78b4",
                "weight": 2,
                "fillOpacity": 0.05,
            },
        ).add_to(fmap)
        minx, miny, maxx, maxy = boundary.total_bounds
        fmap.fit_bounds([[miny, minx], [maxy, maxx]])

    if config.roads_file().exists():
        roads = gpd.read_file(config.roads_file()).to_crs("EPSG:4326")
        if not roads.empty:
            roads = roads[["geometry"]].copy()
            roads["geometry"] = roads.geometry.simplify(0.0001, preserve_topology=True)
            folium.GeoJson(
                roads,
                name="OSM drive roads",
                style_function=lambda _feature: {
                    "color": "#666666",
                    "weight": 1,
                    "opacity": 0.35,
                },
            ).add_to(fmap)

    if config.candidates_geojson_file().exists():
        candidates = gpd.read_file(config.candidates_geojson_file()).to_crs("EPSG:4326")
        if not candidates.empty:
            fields = [
                "rank",
                "score",
                "area_m2",
                "mean_slope_deg",
                "max_slope_deg",
                "nearest_road_m",
                "allowed_landcover_fraction",
                "nearby_slope_score",
            ]
            aliases = [
                "Rank",
                "Score",
                "Area m2",
                "Mean slope deg",
                "Max slope deg",
                "Nearest road m",
                "Allowed land-cover fraction",
                "Nearby slope score",
            ]
            folium.GeoJson(
                candidates,
                name="Candidate sites",
                style_function=lambda feature: {
                    "color": _score_color(feature["properties"].get("score", 0)),
                    "fillColor": _score_color(feature["properties"].get("score", 0)),
                    "weight": 2,
                    "fillOpacity": 0.45,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=[field for field in fields if field in candidates.columns],
                    aliases=aliases[: len([field for field in fields if field in candidates.columns])],
                    localize=True,
                ),
                popup=folium.GeoJsonPopup(
                    fields=[field for field in fields if field in candidates.columns],
                    aliases=aliases[: len([field for field in fields if field in candidates.columns])],
                    localize=True,
                ),
            ).add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)
    output = config.map_file()
    fmap.save(output)
    return output


def _score_color(score: float) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 80:
        return "#1a9850"
    if value >= 60:
        return "#91cf60"
    if value >= 40:
        return "#fee08b"
    if value >= 20:
        return "#fc8d59"
    return "#d73027"
