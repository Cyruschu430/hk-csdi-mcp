"""slice_hk_data: bounded, filtered GeoJSON out — offline. Run: python3 test_slice.py"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hk_csdi_mcp as m

SQ = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}
FEATS = [
    {"type": "Feature", "properties": {"name": "inside", "kind": "A", "pad": "x" * 40},
     "geometry": {"type": "Point", "coordinates": [5, 5]}},
    {"type": "Feature", "properties": {"name": "outside", "kind": "A", "pad": "x" * 40},
     "geometry": {"type": "Point", "coordinates": [50, 50]}},
    {"type": "Feature", "properties": {"name": "other", "kind": "B", "pad": "x" * 40},
     "geometry": {"type": "Point", "coordinates": [9, 9]}},
    {"type": "Feature", "properties": {"name": "shape", "kind": "B", "pad": "x" * 40},
     "geometry": SQ},
    {"type": "Feature", "properties": {"name": "flat", "kind": "B", "pad": "x" * 40},
     "geometry": None},
]
m._cached_features = lambda did: (FEATS, None)


def run(**kw):
    return json.loads(m.slice_hk_data("ds", **kw))


# unfiltered: geometry-bearing features only, the null one is skipped and counted
r = run()
assert r["matched"] == 4 and r["returned"] == 4, r
assert r["geojson"]["type"] == "FeatureCollection" and all(f["geometry"] for f in r["geojson"]["features"])
assert r["crs"] == "EPSG:4326"

# bbox rejects by any vertex (so the polygon still counts) and keeps what is inside
r = run(bbox="0,0,10,10")
assert {f["properties"]["name"] for f in r["geojson"]["features"]} == {"inside", "other", "shape"}, r

# where=FIELD=value is substring, case-insensitive
r = run(where="kind=B")
assert {f["properties"]["name"] for f in r["geojson"]["features"]} == {"other", "shape"}, r

# free text searches every property
r = run(where="IN")
assert {f["properties"]["name"] for f in r["geojson"]["features"]} == {"inside"}, r

# fields= trims properties, geometry stays
r = run(where="kind=A", fields="name")
assert set(r["geojson"]["features"][0]["properties"]) == {"name"}, r

# limit truncates but still reports the true match count
r = run(limit=2)
assert r["returned"] == 2 and r["matched"] == 4 and r["truncated"] is True, r

# byte ceiling: 400 features of ~1KB must stop before SLICE_MAX_BYTES
big = [{"type": "Feature", "properties": {"pad": "y" * 1000, "i": i},
        "geometry": {"type": "Point", "coordinates": [i, i]}} for i in range(400)]
m._cached_features = lambda did: (big, None)
r = run(limit=2000)
assert r["bytes"] <= m.SLICE_MAX_BYTES, r["bytes"]
assert len(r["geojson"]["features"]) < 400 and r["truncated"] is True, r["returned"]

# an attribute-only dataset is refused with a reason, not returned as empty shells
m._cached_features = lambda did: (FEATS[4:], None)
r = run()
assert r["returned"] == 0 and "no geometry" in r["error"], r

# bad bbox is a data error, not a crash
m._cached_features = lambda did: (FEATS, None)
r = run(bbox="10,10,0,0")
assert "error" in r and "inverted" in r["error"], r
r = run(bbox="1,2,3")
assert "error" in r, r

# a filter that matches nothing says so, rather than pretending the dataset is empty
r = run(where="kind=Z")
assert r["returned"] == 0 and r["matched"] == 0, r

print("slice OK: bbox any-vertex, where field/free-text, fields trim, feature+b byte caps, "
      "attribute-only refusal, bad bbox, empty match")
