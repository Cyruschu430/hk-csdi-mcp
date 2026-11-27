"""_arcgis_service: the layer URL ArcGIS Pro can add. Offline (the network is patched)."""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hk_csdi_mcp as m

m.WORK = pathlib.Path(tempfile.mkdtemp(prefix="hk-csdi-svc-"))
DID = "hkfsd_rcd_1634798867463_89696"

SERVICE = {"layers": [
    {"id": 0, "name": "FireStations", "geometryType": "esriGeometryPoint"},
    {"id": 1, "name": "Table", "geometryType": None},   # tables carry no geometry: skip
]}
calls = {"n": 0}


def fake_http(url, data=None, headers=None, timeout=60):
    calls["n"] += 1
    if "/query" in url:                      # the service's own feature count
        return json.dumps({"count": 375193}).encode()
    return json.dumps(SERVICE).encode()


m._http = fake_http

r = m._arcgis_service(DID)
assert r["layer_url"].endswith("/FeatureServer/0"), r
assert r["layer_name"] == "FireStations" and r["geometry_type"] == "esriGeometryPoint", r
assert "ArcGIS Pro cannot add a GeoJSON URL" in r["note"], r
assert r["feature_count"] == 375193, r          # export rows and service features are different populations

# second call is served from the sidecar cache, not the network
before = calls["n"]
assert m._arcgis_service(DID)["layer_url"] == r["layer_url"] and calls["n"] == before


# a service with no feature layers -> None, and the miss is cached for a day only
m._http = lambda *a, **k: json.dumps({"layers": [{"id": 0, "name": "T", "geometryType": None}]}).encode()
# a count that never answers must not lose the layer_url
assert m._arcgis_service("landsd_rcd_1637904048516_16940") is None


# a dead endpoint must return None, never raise into the tool
def boom(*a, **k):
    raise OSError("network down")


m._http = boom
assert m._arcgis_service("some_other_dataset") is None

print("arcgis service OK: layer_url shape, table skipped, cached, empty service, dead endpoint")
