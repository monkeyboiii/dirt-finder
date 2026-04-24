import os

import pytest
from typer.testing import CliRunner

from dirt_finder.cli import app


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_GEO_TESTS") != "1"
    or not os.environ.get("EARTHDATA_USERNAME")
    or not os.environ.get("EARTHDATA_PASSWORD"),
    reason="live geospatial smoke test requires RUN_LIVE_GEO_TESTS=1 and Earthdata credentials",
)
def test_live_cli_help_smoke() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
