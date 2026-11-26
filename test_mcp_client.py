#!/usr/bin/env python3
"""Protocol test: the server must expose exactly these six tools over a real stdio handshake.

    python3 test_mcp_client.py          # needs network (the catalogue + one small dataset)
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

import mcp

ROOT = pathlib.Path(__file__).resolve().parent
EXPECTED = {"search_hk_data", "load_hk_data", "dataset_status",
            "query_hk_data", "summarise_hk_data", "spatial_join", "slice_hk_data"}


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def main() -> int:
    env = dict(os.environ)
    env["HK_CSDI_DIR"] = env.get("HK_CSDI_DIR") or tempfile.mkdtemp(prefix="hk-csdi-test-")
    params = mcp.StdioServerParameters(command=sys.executable,
                                       args=[str(ROOT / "hk_csdi_mcp.py")], env=env)
    async with mcp.Client(params, raise_exceptions=True) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools == EXPECTED, f"tool set drifted: {tools ^ EXPECTED}"

        found = _payload(await client.call_tool("search_hk_data", {"keyword": "電動車充電", "limit": 5}))
        assert found["hits"], found
        assert found["total_datasets"] > 500, found["total_datasets"]
        ids = [h["dataset_id"] for h in found["hits"] if h.get("exportable")]
        assert ids, f"no exportable hit in {found['hits'][:3]}"

        # a metadata-only id must answer with data (an error object), not a protocol exception
        bad = _payload(await client.call_tool("load_hk_data", {"dataset_id": "no_such_dataset"}))
        assert bad.get("error"), bad

        loaded = _payload(await client.call_tool("load_hk_data", {"dataset_id": ids[0]}))
        assert loaded.get("error") or loaded["features"] > 0, loaded

        # the promise to spatial-analysis users: download_url is the real published file, and its
        # feature count matches the summary the model quotes
        dl = loaded["download_url"]
        assert dl.startswith("https://portal.csdi.gov.hk/"), dl
        import urllib.request
        with urllib.request.urlopen(dl, timeout=120) as resp:
            fc = json.load(resp)
        assert fc.get("type") == "FeatureCollection", fc.get("type")
        assert len(fc["features"]) == loaded["features"], (len(fc["features"]), loaded["features"])

        # the analysis path: bounded geometry inline, no second download
        sl = _payload(await client.call_tool("slice_hk_data", {"dataset_id": ids[0], "limit": 3}))
        assert sl["returned"] <= 3 and sl["geojson"]["features"], sl
        assert all(f["geometry"] for f in sl["geojson"]["features"]), sl
        assert sl["bytes"] <= 200_000, sl["bytes"]

        status = _payload(await client.call_tool("dataset_status", {"dataset_id": ids[0]}))
        assert status["checked"] == 1, status

    print(json.dumps({"tools": sorted(EXPECTED), "dataset": ids[0],
                      "features": loaded.get("features"), "status": "ok"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
