from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearchConfig(BaseModel):
    place_name: str = "Hangzhou, Zhejiang, China"
    center_lon: float = 120.1551
    center_lat: float = 30.2741
    drive_time_minutes: int = Field(default=60, gt=0)
    analysis_crs: str = "EPSG:32651"
    graph_fetch_radius_m: int = Field(default=90_000, gt=0)
    isochrone_node_buffer_m: int = Field(default=1_200, gt=0)

    @field_validator("center_lon")
    @classmethod
    def lon_in_range(cls, value: float) -> float:
        if not -180 <= value <= 180:
            raise ValueError("center_lon must be between -180 and 180")
        return value

    @field_validator("center_lat")
    @classmethod
    def lat_in_range(cls, value: float) -> float:
        if not -90 <= value <= 90:
            raise ValueError("center_lat must be between -90 and 90")
        return value


class FilterConfig(BaseModel):
    min_area_m2: float = Field(default=4_047.0, gt=0)
    flat_slope_degrees: float = Field(default=5.0, gt=0)
    max_road_distance_m: float = Field(default=500.0, ge=0)
    allowed_landcover_classes: list[int] = Field(default_factory=lambda: [30, 60])
    min_allowed_landcover_fraction: float = Field(default=0.6, ge=0, le=1)
    nearby_slope_buffer_m: float = Field(default=500.0, ge=0)
    target_area_m2: float = Field(default=20_000.0, gt=0)

    @model_validator(mode="after")
    def target_area_not_smaller_than_min(self) -> FilterConfig:
        if self.target_area_m2 < self.min_area_m2:
            raise ValueError("target_area_m2 must be greater than or equal to min_area_m2")
        return self


class ScoringWeights(BaseModel):
    area: float = Field(default=0.25, ge=0)
    flatness: float = Field(default=0.25, ge=0)
    road_access: float = Field(default=0.20, ge=0)
    vegetation: float = Field(default=0.20, ge=0)
    nearby_slope: float = Field(default=0.10, ge=0)

    @model_validator(mode="after")
    def total_must_be_positive(self) -> ScoringWeights:
        if self.total <= 0:
            raise ValueError("at least one scoring weight must be positive")
        return self

    @property
    def total(self) -> float:
        return self.area + self.flatness + self.road_access + self.vegetation + self.nearby_slope


class PathConfig(BaseModel):
    cache_dir: Path = Path("data/cache")
    output_dir: Path = Path("outputs")
    dem_path: Path | None = None
    landcover_path: Path | None = None
    boundary_path: Path | None = None
    roads_path: Path | None = None
    search_area_path: Path | None = None

    @field_validator(
        "cache_dir",
        "output_dir",
        "dem_path",
        "landcover_path",
        "boundary_path",
        "roads_path",
        "search_area_path",
        mode="before",
    )
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: SearchConfig = Field(default_factory=SearchConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    paths: PathConfig = Field(default_factory=PathConfig)

    def resolved_cache_dir(self) -> Path:
        return self.paths.cache_dir

    def resolved_output_dir(self) -> Path:
        return self.paths.output_dir

    def boundary_file(self) -> Path:
        return self.paths.boundary_path or self.paths.cache_dir / "osm" / "hangzhou_boundary.geojson"

    def roads_file(self) -> Path:
        return self.paths.roads_path or self.paths.cache_dir / "osm" / "drive_roads.geojson"

    def graph_file(self) -> Path:
        return self.paths.cache_dir / "osm" / "drive_graph.graphml"

    def isochrone_file(self) -> Path:
        return self.paths.cache_dir / "osm" / "drive_isochrone.geojson"

    def search_area_file(self) -> Path:
        return self.paths.search_area_path or self.paths.cache_dir / "osm" / "search_area.geojson"

    def dem_file(self) -> Path:
        return self.paths.dem_path or self.paths.cache_dir / "dem" / "dem_mosaic.tif"

    def landcover_file(self) -> Path:
        return self.paths.landcover_path or self.paths.cache_dir / "landcover" / "worldcover_mosaic.tif"

    def candidates_geojson_file(self) -> Path:
        return self.paths.output_dir / "candidates.geojson"

    def candidates_csv_file(self) -> Path:
        return self.paths.output_dir / "candidates.csv"

    def map_file(self) -> Path:
        return self.paths.output_dir / "map.html"

    def metadata_file(self) -> Path:
        return self.paths.output_dir / "run_metadata.json"


def hangzhou_config() -> AppConfig:
    return AppConfig()


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as file:
        raw = tomllib.load(file)
    return AppConfig.model_validate(raw)


def write_config(config: AppConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_toml(config), encoding="utf-8")


def to_toml(config: AppConfig) -> str:
    data = config.model_dump(mode="json", exclude_none=True)
    sections = []
    for section_name in ["search", "filters", "weights", "paths"]:
        values = data[section_name]
        lines = [f"[{section_name}]"]
        if section_name == "paths":
            lines.extend(
                [
                    "# Optional manual overrides:",
                    '# dem_path = "/absolute/path/to/dem.tif"',
                    '# landcover_path = "/absolute/path/to/worldcover.tif"',
                ]
            )
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
