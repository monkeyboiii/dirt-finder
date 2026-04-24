import geopandas as gpd
from shapely.geometry import LineString

from dirt_finder.fetch import _clean_roads_for_geojson, worldcover_tile_ids_for_bounds, worldcover_url


def test_worldcover_tile_ids_cover_crossing_bounds() -> None:
    tiles = worldcover_tile_ids_for_bounds((119.9, 29.9, 120.2, 30.2))

    assert tiles == ["N27E117", "N27E120", "N30E117", "N30E120"]


def test_worldcover_url_uses_public_2021_v200_map_bucket() -> None:
    url = worldcover_url("N30E120")

    assert url == (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
        "ESA_WorldCover_10m_2021_v200_N30E120_Map.tif"
    )


def test_clean_roads_for_geojson_serializes_list_attributes() -> None:
    roads = gpd.GeoDataFrame(
        {
            "u": [1],
            "v": [2],
            "osmid": [[10, 11]],
            "highway": [["primary", "secondary"]],
            "unneeded": [{"nested": True}],
        },
        geometry=[LineString([(0, 0), (1, 1)])],
        crs="EPSG:4326",
    )

    cleaned = _clean_roads_for_geojson(roads)

    assert "unneeded" not in cleaned.columns
    assert cleaned.loc[0, "osmid"] == "10; 11"
    assert cleaned.loc[0, "highway"] == "primary; secondary"
