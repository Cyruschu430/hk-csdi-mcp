"""spatial_join: pure-Python point-in-polygon, no network. Run: python3 test_spatial_join.py"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hk_csdi_mcp as m

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}
# a square with a hole: (4..6, 4..6) is outside the polygon
HOLEY = {"type": "Polygon", "coordinates": [
    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
    [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
]}
# two disjoint parts, 20..21 and 25..26, with the point on the second one only
MULTI = {"type": "MultiPolygon", "coordinates": [
    [[[20, 0], [21, 0], [21, 1], [20, 1], [20, 0]]],
    [[[25, 0], [26, 0], [26, 1], [25, 1], [25, 0]]],
]}

POINTS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [5, 5]}},
    {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [9.9, 9.9]}},
    {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [50, 50]}},
    {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}},
]}


def run(polys, field="name"):
    feats = [{"type": "Feature", "properties": p["props"], "geometry": p["geom"]} for p in polys]
    m._cached_features = lambda did: (POINTS["features"] if did == "pts" else feats, None)
    return json.loads(m.spatial_join("pts", "polys", field))


def sq(name="sq", **extra):
    return {"name": name, "geom": SQUARE, "props": {"name": name, **extra}}


r = run([sq()])
assert r["rows"][0]["count"] == 2, r["rows"]          # 5,5 and 9.9,9.9 inside
assert r["matched"] == 2 and r["unmatched"] == 1, r   # 50,50 outside
assert r["skipped"]["non_point_features"] == 1, r     # the LineString is skipped, not counted

r = run([{"name": "hole", "geom": HOLEY, "props": {"name": "hole"}}])
assert r["rows"][0]["count"] == 1, r["rows"]          # 5,5 falls in the hole, 9.9,9.9 does not

r = run([{"name": "a", "geom": MULTI, "props": {"name": "a"}}])
assert r["rows"][0]["count"] == 0, r["rows"]          # neither part contains the points
assert r["unmatched"] == 3, r

r = run([sq(), {"name": "a", "geom": MULTI, "props": {"name": "a"}}])
assert {row["group"]: row["count"] for row in r["rows"]} == {"sq": 2, "a": 0}, r["rows"]
assert r["matched"] == 2, r

# no field -> overview, never a guess
r = run([sq()], field="")
assert "label_fields" in r and "hint" in r and "rows" not in r, r

# unknown field -> error, not a crash
r = run([sq()], field="nope")
assert "error" in r and r["label_fields"] == ["name"], r

# a readable name is offered before a code, because label_fields[0] is what a model picks
r = run([{"name": "x", "geom": SQUARE, "props": {"AREA_CODE": "SK", "NAME_EN": "Sham Shui Po"}},
         {"name": "y", "geom": SQUARE, "props": {"AREA_CODE": "TP", "NAME_EN": "Tai Po"}}], field="")
assert r["label_fields"][0] == "NAME_EN", r["label_fields"]

print("spatial_join OK: bbox reject, hole parity, multipolygon, skip non-points, overview, "
      "bad field, name-before-code ordering")
