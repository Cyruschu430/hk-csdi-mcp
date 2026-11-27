"""arcgis_query_layer: a filtered service query, not a whole-layer add. Offline."""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hk_csdi_mcp as m

m.WORK = pathlib.Path(tempfile.mkdtemp(prefix="hk-csdi-q-"))
DID = "landsd_rcd_1637217253134_22729"
SERVICE = {"layers": [{"id": 0, "name": "Lot", "geometryType": "esriGeometryPolygon"}]}


def fake_http(url, data=None, headers=None, timeout=60):
    if "returnCountOnly" in url:
        return json.dumps({"count": 28176}).encode()
    return json.dumps(SERVICE).encode()


m._http = fake_http

r = json.loads(m.arcgis_query_layer(DID, bbox="113.82,22.19,114.06,22.32"))
assert r["count"] == 28176 and r["page_size"] == 3000 and r["pages_needed"] == 10, r
assert "geometryType=esriGeometryEnvelope" in r["query_url"], r["query_url"]
assert "spatialRel=esriSpatialRelIntersects" in r["query_url"]
assert "f=geojson" in r["query_url"] and "resultRecordCount=3000" in r["query_url"]
assert "JSONToFeatures" in r["arcpy_snippet"] and "resultOffset" in r["arcpy_snippet"], r["arcpy_snippet"]
assert r["layer_url"].endswith("/FeatureServer/0"), r

# a page size above the service cap is clamped, not forwarded
r = json.loads(m.arcgis_query_layer(DID, bbox="113.82,22.19,114.06,22.32", page_size=99999))
assert r["page_size"] == m.PAGE, r["page_size"]

# where clause is carried through and count=0 is an answer, not an empty success
m._http = lambda url, data=None, headers=None, timeout=60: (
    json.dumps({"count": 0}).encode() if "returnCountOnly" in url else json.dumps(SERVICE).encode())
r = json.loads(m.arcgis_query_layer(DID, where="LotCSUID='nope'"))
assert r["count"] == 0 and "no features" in r["error"], r

# a dataset with no service layer is refused with advice
m._http = lambda url, data=None, headers=None, timeout=60: json.dumps({"layers": []}).encode()
r = json.loads(m.arcgis_query_layer("some_dataset"))
assert "no published FeatureServer" in r["error"], r

# a bad bbox never reaches the service
r = json.loads(m.arcgis_query_layer(DID, bbox="10,10,0,0"))
assert "inverted" in r["error"], r

print("arcgis_query_layer OK: count+pages, envelope+spatialRel in URL, paging snippet, "
      "page clamp, zero match, no service, bad bbox")
