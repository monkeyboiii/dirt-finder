from pathlib import Path

import pytest

from dirt_finder.config import AppConfig, FilterConfig, hangzhou_config, load_config, write_config


def test_hangzhou_defaults_match_plan() -> None:
    config = hangzhou_config()

    assert config.search.center_lon == pytest.approx(120.1551)
    assert config.search.center_lat == pytest.approx(30.2741)
    assert config.search.drive_time_minutes == 60
    assert config.search.analysis_crs == "EPSG:32651"
    assert config.filters.min_area_m2 == pytest.approx(4047)
    assert config.filters.flat_slope_degrees == pytest.approx(5)
    assert config.filters.max_road_distance_m == pytest.approx(500)
    assert config.filters.allowed_landcover_classes == [30, 60]


def test_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "hangzhou.toml"
    write_config(hangzhou_config(), path)

    loaded = load_config(path)

    assert isinstance(loaded, AppConfig)
    assert loaded.search.place_name == "Hangzhou, Zhejiang, China"
    assert loaded.paths.output_dir == Path("outputs")


def test_filter_target_area_must_cover_minimum_area() -> None:
    with pytest.raises(ValueError):
        FilterConfig(min_area_m2=10_000, target_area_m2=5_000)
