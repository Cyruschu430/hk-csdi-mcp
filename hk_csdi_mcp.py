#!/usr/bin/env python3
"""hk-csdi-mcp — Hong Kong CSDI spatial data for any MCP client.

Six tools, one file, stdlib + `mcp`. Runs locally (stdio) or as a remote server
(streamable HTTP) behind a bearer token.

    python3 hk_csdi_mcp.py                                  # stdio (Claude Desktop, Cursor…)
    HK_CSDI_TRANSPORT=streamable-http python3 hk_csdi_mcp.py # remote, needs tokens
    python3 hk_csdi_mcp.py --add-token alice                # mint a token, print it once
    python3 hk_csdi_mcp.py --list-tokens
    python3 hk_csdi_mcp.py --revoke-token alice
    python3 hk_csdi_mcp.py --selftest

Env:
  HK_CSDI_DIR              cache/workspace dir (default ./hk-csdi)
  HK_CSDI_STALE_DAYS       re-download a cached dataset older than this (default 30)
  HK_CSDI_TRANSPORT        stdio (default) | streamable-http
  HK_CSDI_HOST             bind address (default 127.0.0.1 — put a TLS proxy in front)
  HK_CSDI_PORT             bind port (default 8788)
  HK_CSDI_PATH             HTTP path (default /mcp)
  HK_CSDI_TOKENS           token file (default ./tokens.json)
  HK_CSDI_RPM              requests per minute per token (default 60; 0 = unlimited)
  HK_CSDI_LOADS_PER_HOUR   dataset downloads per token per hour (default 30; 0 = unlimited)

The dataset catalogue, the GeoJSON exports and the search index all come from the
CSDI portal (portal.csdi.gov.hk). Data ownership and attribution stay with the
publishing government departments.
"""
import collections
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape as _xml_escape

CSDI_CSW = "https://portal.csdi.gov.hk/geoportal/csw"
CSDI_FILE = "https://portal.csdi.gov.hk/csdi-webpage/file-api?dataset_id={}&format=geojson"
WORK = pathlib.Path(os.environ.get("HK_CSDI_DIR") or "hk-csdi").resolve()
TOKENS = pathlib.Path(os.environ.get("HK_CSDI_TOKENS") or "tokens.json").resolve()
STALE_DAYS = float(os.environ.get("HK_CSDI_STALE_DAYS") or 30)
RPM = int(os.environ.get("HK_CSDI_RPM") or 60)
LOADS_PER_HOUR = int(os.environ.get("HK_CSDI_LOADS_PER_HOUR") or 30)

CSW_BODY = """<?xml version="1.0"?><csw:GetRecords service="CSW" version="2.0.2" maxRecords="{n}" resultType="results" outputSchema="http://www.opengis.net/cat/csw/2.0.2" xmlns:csw="http://www.opengis.net/cat/csw/2.0.2"><csw:Query typeNames="csw:Record"><csw:ElementSetName>summary</csw:ElementSetName><csw:Constraint version="1.1.0"><ogc:Filter xmlns:ogc="http://www.opengis.net/ogc"><ogc:PropertyIsLike wildCard="%" singleChar="_" escapeChar="\\"><ogc:PropertyName>AnyText</ogc:PropertyName><ogc:Literal>%{kw}%</ogc:Literal></ogc:PropertyIsLike></ogc:Filter></csw:Constraint></csw:Query></csw:GetRecords>"""

CSW_ALL = """<?xml version="1.0"?><csw:GetRecords service="CSW" version="2.0.2" maxRecords="{n}" startPosition="1" resultType="results" outputSchema="http://www.opengis.net/cat/csw/2.0.2" xmlns:csw="http://www.opengis.net/cat/csw/2.0.2"><csw:Query typeNames="csw:Record"><csw:ElementSetName>full</csw:ElementSetName></csw:Query></csw:GetRecords>"""


def _http(url, data=None, headers=None, timeout=60):
    # ponytail: one retry. Public CSDI endpoints return transient 5xx and reset
    # connections under load; a second attempt beats teaching every caller to retry.
    last = None
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 or attempt == 2:
                raise
            time.sleep(1.5)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e  # timeouts and resets arrive here, not as HTTPError
            if attempt == 2:
                raise
            time.sleep(1.5)
    raise last


def _save(path, text: str) -> None:
    """Atomic write: several clients share one cache dir, and a torn catalog.json
    breaks every later search until the file is deleted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 11:
                path.write_text(text, encoding="utf-8")  # non-atomic beats failing the call
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return
            time.sleep(0.04 * (attempt + 1))


# ---------------------------------------------------------------- CSDI catalogue

# Hong Kong Chinese term -> catalogue wording. CSDI titles are often 洗手間 where users say 廁所.
SYNONYMS = {
    "廁所": "洗手間 toilets", "洗手間": "toilets", "公廁": "洗手間 toilets",
    "樓宇": "building 大廈", "大廈": "building 樓宇", "建築物": "building",
    "公立醫院": "hospital authority 醫院", "私家醫院": "private hospital 醫院",
    "行人路": "pedestrian footpath walkway 行人", "行人": "pedestrian 行人路",
    "巴士": "bus 巴士站", "港鐵": "mtr railway 鐵路", "渡輪": "ferry 碼頭",
    "學校": "school 學校", "醫院": "hospital 醫院", "診所": "clinic 診所",
    "垃圾": "waste refuse 廢物", "回收": "recycling 回收", "樹": "tree 樹木",
    "充電": "charging 電動車", "電動車": "electric vehicle charging 電動車",
    "雨量": "rainfall 雨量", "降雨": "rainfall 雨量", "斜坡": "slope 斜坡",
    "郊野公園": "country park 郊野公園", "海岸公園": "marine park 海岸",
    "停車場": "parking car park 停車", "海灘": "beach 泳灘", "泳灘": "beach",
    "單車": "cycling bicycle 單車徑", "渠務": "drainage sewer 渠",
    "水管": "water main 水管", "煤氣": "gas 氣體", "電纜": "cable 電纜",
    "地契": "lease 地段", "分區計劃大綱": "outline zoning plan 分區計劃大綱",
    "屋邨": "estate 屋邨", "公屋": "public housing 公營房屋", "私樓": "private housing",
    "火警": "fire 消防", "消防局": "fire station 消防局", "警署": "police 警署",
    "街市": "market 街市", "墳場": "cemetery columbarium 墳",
    "空氣": "air quality 空氣", "水質": "water quality 水質", "山泥傾瀉": "landslide 山泥傾瀉",
}


def _catalog(force: bool = False) -> list:
    """The WHOLE CSDI catalogue (1150+ datasets) in one request, cached on disk for 7 days.
    Full element set = English + 繁體/简体 titles, keywords and abstracts."""
    cache = WORK / "catalog.json"
    if not force and cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # torn cache: re-harvest below
    body = CSW_ALL.format(n=5000).encode()
    xml = _http(CSDI_CSW, body, {"Content-Type": "application/xml"}, timeout=300).decode("utf-8", "replace")
    out = []
    for rec in re.findall(r"<csw:Record>(.*?)</csw:Record>", xml, re.S):
        g = lambda t: re.sub(r"\s+", " ", "".join(re.findall(rf"<{t}>(.*?)</{t}>", rec, re.S))).strip()
        did = g("dc:identifier")
        if not did:
            continue
        out.append({
            "id": did, "title": g("dc:title"), "title_zh": g("dc:title_tc") or g("dc:title_sc"),
            "kw": [k.strip() for k in re.findall(r'<dc:subject scheme="keywords">(.*?)</dc:subject>', rec, re.S)],
            "abstract": g("dct:abstract")[:300], "abstract_zh": (g("dct:abstract_tc") or g("dct:abstract_sc"))[:300],
            "org": g("dc:creator"), "updated": g("dct:modified")[:10],
            "exportable": "_rcd_" in did,  # 13 metadata-only datasets 404 on export
        })
    if not out:
        raise RuntimeError("CSDI catalogue harvest returned 0 records (endpoint changed?)")
    _save(cache, json.dumps(out, ensure_ascii=False))
    return out


def _score(query: str, d: dict) -> int:
    q = query.lower().strip()
    terms = [t for t in re.split(r"\s+", q) if t]
    # expand any HK term that appears INSIDE the query (公立醫院 -> 醫院 -> hospital)
    for k, v in SYNONYMS.items():
        if k in q:
            terms += [w for w in v.split() if w]
    terms = list(dict.fromkeys(terms))
    title = f"{d['title']} {d['title_zh']}".lower()
    kw = " ".join(d["kw"]).lower()
    abst = f"{d['abstract']} {d['abstract_zh']}".lower()
    s = 0
    for t in terms:
        s += 8 * (t in title) + 4 * (t in kw) + 2 * (t in abst)
        s += 4 * (title.startswith(t) or title.split(" ")[0] == t)
    return s


def search_hk_data(keyword: str = "", limit: int = 10, offset: int = 0) -> str:
    """Search ALL ~1150 CSDI datasets locally (English or Chinese), ranked. No keyword = browse.

    Chinese and English both work — Chinese terms are expanded with Hong Kong usage
    (廁所 -> 洗手間, 行人路 -> pedestrian, 電動車 -> electric vehicle charging, …).
    Returns dataset_id + bilingual title + why it matched. Feed dataset_id to load_hk_data().
    """
    cat = _catalog()
    limit, offset = max(1, min(limit, 30)), max(0, offset)
    if not keyword.strip():
        page = cat[offset:offset + limit]
        return json.dumps({"total_datasets": len(cat), "offset": offset,
                           "hits": [{"dataset_id": d["id"], "title": d["title"],
                                     "title_zh": d["title_zh"], "org": d["org"]} for d in page]},
                          ensure_ascii=False)
    # shorter title breaks ties: "Building" beats "Quality Water Supply Scheme for Buildings – …"
    ranked = sorted(((s, d) for d in cat if (s := _score(keyword, d)) > 0),
                    key=lambda kv: (-kv[0], len(kv[1]["title"]), kv[1]["title"]))
    if not ranked:  # 斜坡安全 -> 斜坡: drop trailing CJK chars until something matches
        for n in range(len(keyword.strip()) - 1, 1, -1):
            ranked = sorted(((s, d) for d in cat if (s := _score(keyword.strip()[:n], d)) > 0),
                            key=lambda kv: (-kv[0], len(kv[1]["title"]), kv[1]["title"]))
            if ranked:
                break
    hits = [{"dataset_id": d["id"], "title": d["title"], "title_zh": d["title_zh"],
             "org": d["org"], "updated": d["updated"], "score": s,
             "exportable": d["exportable"],
             "match": d["abstract"][:150] or d["abstract_zh"][:150]}
            for s, d in ranked[offset:offset + limit]]
    if not hits and offset == 0:  # nothing local -> ask the catalogue server (broad recall)
        # `&`/`<` in a keyword used to reach the server as raw XML and come back 403,
        # which reads like an outage instead of a bad query. Escape at the boundary.
        body = CSW_BODY.format(n=limit, kw=_xml_escape(keyword.replace("%", ""))).encode()
        try:
            xml = _http(CSDI_CSW, body, {"Content-Type": "application/xml"}).decode("utf-8", "replace")
        except Exception as e:
            return json.dumps({"query": keyword, "matched": 0, "total_datasets": len(cat), "hits": [],
                               "note": f"the local index had no match and the catalogue server refused "
                                       f"the fallback query ({type(e).__name__}: {e}) — retry with a "
                                       f"synonym or a shorter Chinese term"},
                              ensure_ascii=False)
        hits = [{"dataset_id": t[0], "title": re.sub(r"\s+", " ", t[1]).strip(), "source": "csw"}
                for t in ((i, x) for i, x in zip(re.findall(r"<dc:identifier>(.*?)</dc:identifier>", xml, re.S),
                                                 re.findall(r"<dc:title>(.*?)</dc:title>", xml, re.S)))]
    return json.dumps({"query": keyword, "matched": len(ranked),
                       "total_datasets": len(cat), "hits": hits,
                       "next": "load_hk_data(dataset_id), then query_hk_data / summarise_hk_data"},
                      ensure_ascii=False)


def load_hk_data(dataset_id: str, refresh: bool = False) -> str:
    """Download one CSDI dataset as GeoJSON (WGS84) and cache it on the server.

    Returns a SUMMARY, not the data itself — never paste features into the chat; use
    query_hk_data() for a few rows and summarise_hk_data() for aggregates. A cached copy
    is reused (and shared with other callers) until it is HK_CSDI_STALE_DAYS old;
    refresh=True re-downloads now.
    """
    path = WORK / "data" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', dataset_id)}.geojson"
    age = (time.time() - path.stat().st_mtime) / 86400 if path.exists() else 0.0
    cached, fc, warning = path.exists() and not refresh and age <= STALE_DAYS, None, ""
    if cached:
        try:
            fc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # truncated/partial file: fall through to a re-download
            cached = False
    if fc is None:
        try:
            fc = json.loads(_http(CSDI_FILE.format(urllib.parse.quote(dataset_id)), timeout=180))
        except urllib.error.HTTPError as e:
            if path.exists():  # never drop a good copy because a refresh failed
                fc = json.loads(path.read_text(encoding="utf-8"))
                warning = (f"re-download failed (HTTP {e.code}); serving the cached copy from "
                           f"{age:.0f} days ago — it may no longer match the published dataset")
            else:
                return json.dumps({
                    "error": f"{dataset_id}: HTTP {e.code} from the CSDI export endpoint",
                    "likely": "metadata-only / non-spatial dataset (13 of ~1150 have no GeoJSON export), "
                              "or the id is wrong — call search_hk_data() and use a hit whose exportable is true",
                    "metadata_page": f"https://portal.csdi.gov.hk/geoportal/rest/metadata/item/{dataset_id}/html",
                }, ensure_ascii=False)
    feats = fc.get("features") or []
    if not feats:
        return json.dumps({"error": f"{dataset_id}: dataset returned 0 features (not spatial or empty)"})
    if not cached:
        _save(path, json.dumps(fc, ensure_ascii=False))
        age = 0.0
    lons, lats = [], []
    for f in feats:
        for x, y in _coords(f.get("geometry") or {}):
            lons.append(x), lats.append(y)
    # Some CSDI exports are attribute-only: `geometry` is null on every feature, so
    # `features` looks healthy while nothing can be placed. LandSD "Building" is
    # 87,982 features / 0 geometries (verified live 2026-11-05), "Lot" 1,606 / 0.
    if not lons:
        return json.dumps({
            "dataset_id": dataset_id, "features": len(feats), "geometry": ["none"],
            "detail": f"{len(feats)} features, but every one carries `geometry: null` — attribute-only, "
                      f"so no spatial operation can place it",
            "still_usable": "query_hk_data() and summarise_hk_data() work normally on the cached copy",
            "download_url": CSDI_FILE.format(urllib.parse.quote(dataset_id)),
            "known": ["LandSD Building — 87,982 features, 0 geometries",
                      "LandSD Lot — 1,606 features, 0 geometries"],
        }, ensure_ascii=False)
    out = {
        "dataset_id": dataset_id, "features": len(feats),
        "geometry": sorted({(f.get("geometry") or {}).get("type") or "?" for f in feats}),
        "fields": list((feats[0].get("properties") or {}).keys())[:20],
        "bbox": [round(min(lons), 5), round(min(lats), 5), round(max(lons), 5), round(max(lats), 5)],
        "cached": cached, "cached_days": round(age, 1) if cached else 0,
        # The features stay on this side on purpose (87k-feature datasets would flood a chat).
        # Whoever wants the file itself fetches the published one — same bytes, straight from CSDI.
        "download_url": CSDI_FILE.format(urllib.parse.quote(dataset_id)),
        "download_format": "GeoJSON, WGS84 / EPSG:4326 — the published export, unchanged",
        "next": "query_hk_data(dataset_id, where=...) for rows, summarise_hk_data(dataset_id, "
                "group_by=...) for aggregates, spatial_join(points_dataset=..., polygons_dataset=...) "
                "to count points inside polygons",
    }
    if warning:
        out["warning"] = warning
    return json.dumps(out, ensure_ascii=False)


def _age_days(date_str: str):
    """'2026-03-14' -> days since. None if the catalogue gave nothing usable."""
    try:
        return round((time.time() - time.mktime(time.strptime(date_str[:10], "%Y-%m-%d"))) / 86400, 1)
    except (ValueError, TypeError, OverflowError):
        return None


def _probe_export(dataset_id: str, timeout: int = 15) -> dict:
    """Is the CSDI export endpoint still serving THIS dataset? Reads 2 KB and hangs up."""
    url = CSDI_FILE.format(urllib.parse.quote(dataset_id))
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hk-csdi-mcp"}),
                                    timeout=timeout) as r:
            head = r.read(2048)
        ms = int((time.time() - t0) * 1000)
        if head.lstrip()[:1] in (b"{", b"["):
            return {"http": r.status, "ms": ms, "body": "geojson"}
        return {"http": r.status, "ms": ms, "body": "unexpected",
                "sample": head[:80].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"http": e.code, "ms": int((time.time() - t0) * 1000), "body": "error",
                "note": "HTTP error — likely metadata-only (no GeoJSON export) or a wrong id"}
    except Exception as e:  # timeout, DNS, TLS — all mean "not serving right now"
        return {"http": None, "ms": int((time.time() - t0) * 1000), "body": "unreachable",
                "note": f"{type(e).__name__}: {str(e)[:120]}"}


def dataset_status(dataset_id: str = "", probe: bool = False, limit: int = 10,
                   stale_after_days: int = 365) -> str:
    """Are these datasets still being served, and how stale are they?

    No dataset_id -> lists the `limit` oldest datasets in the catalogue, so you can see drift at a
    glance. probe=True also calls the CSDI export endpoint to confirm it answers right now (reads
    2 KB only, costs a round trip each — capped at 10 per call).

    The date below is CSDI's dct:modified (when the record was revised), NOT the statistical
    reference period of the data.
    """
    cat = _catalog()
    limit = max(1, min(limit, 30))
    if dataset_id:
        picked = [d for d in cat if d["id"] == dataset_id]
        if not picked:
            near = [d["id"] for d in cat if dataset_id[:12] in d["id"]][:5]
            return json.dumps({"error": f"{dataset_id} not in the catalogue", "similar_ids": near,
                               "next": "search_hk_data(keyword) to get a valid dataset_id"},
                              ensure_ascii=False)
    else:
        picked = sorted(cat, key=lambda d: d["updated"] or "9999")[:limit]

    rows, probed = [], 0
    for d in picked:
        age = _age_days(d["updated"])
        row = {
            "dataset_id": d["id"], "title": d["title"][:80], "title_zh": d["title_zh"][:40] or None,
            "org": d["org"], "catalogue_updated": d["updated"] or None,
            "age_days": age,
            "state": ("stale" if age is not None and age > stale_after_days
                      else "aging" if age is not None and age > stale_after_days / 2
                      else "fresh" if age is not None else "unknown"),
            "exportable": d["exportable"],
        }
        if probe and d["exportable"] and probed < 10:
            row["live"] = _probe_export(d["id"])
            probed += 1
        rows.append(row)
    return json.dumps({
        "checked": len(rows), "probed": probed,
        "dates_basis": "CSDI catalogue dct:modified — record revision date, NOT the data's reference period",
        "stale_after_days": stale_after_days,
        "stale": sum(1 for r in rows if r["state"] == "stale"),
        "datasets": rows,
        "next": "load_hk_data(dataset_id, refresh=True) to re-pull anything that moved",
    }, ensure_ascii=False)


def _coords(geom):
    """Yield (lon, lat) from any GeoJSON geometry — point, line, polygon, multi."""
    t, c = geom.get("type"), geom.get("coordinates")
    if not c:
        return
    if t == "Point":
        yield c[0], c[1]
    elif t in ("MultiPoint", "LineString"):
        for p in c:
            yield p[0], p[1]
    elif t in ("MultiLineString", "Polygon"):
        for ring in c:
            for p in ring:
                yield p[0], p[1]
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                for p in ring:
                    yield p[0], p[1]


def _cached_features(dataset_id: str):
    """Features of a dataset, from the server cache (downloads only if missing)."""
    p = WORK / "data" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', dataset_id)}.geojson"
    if not p.exists():
        r = json.loads(load_hk_data(dataset_id))
        if r.get("error"):
            return None, r
    return json.loads(p.read_text(encoding="utf-8")).get("features") or [], None


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


SLICE_MAX_FEATURES = 2000   # ponytail: fixed ceiling; a model analysing >2k rows in-context is already losing
SLICE_MAX_BYTES = 200_000   # ~50k tokens of GeoJSON — a slice, not a download


def _parse_bbox(bbox: str):
    """"minLon,minLat,maxLon,maxLat" or a 4-item list -> tuple, else ValueError."""
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        vals = [float(v) for v in bbox]
    else:
        parts = [p for p in re.split(r"[,\s]+", str(bbox).strip()) if p]
        if len(parts) != 4:
            raise ValueError("bbox must be 4 numbers: minLon,minLat,maxLon,maxLat (WGS84)")
        vals = [float(p) for p in parts]
    w, s, e, n = vals
    if not (w < e and s < n):
        raise ValueError(f"bbox corners are inverted: {vals}")
    if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90):
        raise ValueError(f"bbox outside WGS84 range: {vals}")
    return w, s, e, n


def slice_hk_data(dataset_id: str, bbox: str = "", where: str = "", fields: str = "",
                  limit: int = 200) -> str:
    """Return a BOUNDED GeoJSON FeatureCollection — real geometry, ready to analyse now.

    Use this when the analysis needs the coordinates, not just a count: filter to a bbox
    ("minLon,minLat,maxLon,maxLat") and/or a `where` clause (FIELD=value, or free text), and up to
    `limit` features come back inline with their geometry. Capped at 2,000 features / ~200 KB;
    `truncated: true` means narrow the filter or use download_url for the whole file.

    Datasets whose features carry no geometry are refused here (an attribute-only dataset cannot be
    mapped or joined) — the refusal names that, instead of returning empty shells.
    """
    feats, err = _cached_features(dataset_id)
    if err:
        return json.dumps(err, ensure_ascii=False)
    if not feats:
        return json.dumps({"error": f"{dataset_id}: no features"})
    try:
        cap = max(1, min(int(limit), SLICE_MAX_FEATURES))
    except (TypeError, ValueError):
        cap = 200

    box = None
    if str(bbox).strip():
        try:
            box = _parse_bbox(bbox)
        except ValueError as e:
            return json.dumps({"error": f"bbox: {e}", "expected": "minLon,minLat,maxLon,maxLat"}, ensure_ascii=False)

    field_list = [k for k in ((feats[0].get("properties") or {})) if not k.startswith("__")]
    cond = (where or "").strip()
    field_name, needle = "", cond.lower()
    if "=" in cond:
        field_name, _, needle = [s.strip() for s in cond.partition("=")]
        if field_name not in field_list:
            return json.dumps({"error": f"{dataset_id}: no field {field_name!r}", "fields": field_list[:25]},
                              ensure_ascii=False)
        needle = needle.lower()
    keep = [s.strip() for s in fields.split(",") if s.strip()]

    matched = kept = 0
    no_geometry = truncated = 0
    out, size = [], 0
    for f in feats:
        props = f.get("properties") or {}
        if field_name:
            if needle not in str(props.get(field_name) or "").lower():
                continue
        elif cond and not any(needle in str(v).lower() for v in props.values() if v is not None):
            continue
        if box:
            coords = [(x, y) for x, y in _coords(f.get("geometry") or {})]
            if not coords:
                no_geometry += 1
                continue
            if not any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in coords):
                continue
        elif (f.get("geometry") or {}).get("coordinates") is None:
            no_geometry += 1
            continue
        matched += 1
        if len(out) >= cap:
            truncated += 1
            continue
        picked = {k: v for k, v in props.items() if not k.startswith("__") and (not keep or k in keep)}
        feature = {"type": "Feature", "geometry": f["geometry"], "properties": picked}
        blob = json.dumps(feature, ensure_ascii=False)
        if size + len(blob) > SLICE_MAX_BYTES and out:
            truncated += 1
            continue
        out.append(feature)
        size += len(blob)
        kept += 1

    if not out:
        reason = (f"{no_geometry} matching features carry no geometry (attribute-only dataset)"
                  if no_geometry else "nothing matched the bbox/where filter")
        return json.dumps({
            "dataset_id": dataset_id, "returned": 0, "matched": matched, "no_geometry": no_geometry,
            "error": reason,
            "next": "drop or widen the bbox/where filter, or use query_hk_data / summarise_hk_data "
                    "for the attributes without geometry",
        }, ensure_ascii=False)
    return json.dumps({
        "dataset_id": dataset_id, "matched": matched, "returned": kept,
        "truncated": truncated > 0,
        "limits": {"features": cap, "bytes": SLICE_MAX_BYTES},
        "bytes": size, "crs": "EPSG:4326",
        "geojson": {"type": "FeatureCollection", "features": out},
        "note": "a slice, not the dataset — download_url from load_hk_data has every feature",
        "next": "analyse these features directly (plot, buffer, join, export), or narrow bbox/where "
                "for a different slice",
    }, ensure_ascii=False)


def query_hk_data(dataset_id: str, where: str = "", fields: str = "", limit: int = 5) -> str:
    """Read a FEW individual features, so a single value can be verified without dumping a dataset.

    `where` is either `FIELD=value` (case-insensitive match on one field) or bare text searched
    across all fields. Rows are capped at 20 — use summarise_hk_data for aggregates.
    """
    feats, err = _cached_features(dataset_id)
    if err:
        return json.dumps(err, ensure_ascii=False)
    if not feats:
        return json.dumps({"error": f"{dataset_id}: no features"})
    cap = max(1, min(int(limit), 20))
    field_list = [k for k in ((feats[0].get("properties") or {})) if not k.startswith("__")]
    cond = (where or "").strip()
    field_name, needle = "", cond.lower()
    if "=" in cond:
        field_name, _, needle = [s.strip() for s in cond.partition("=")]
        if field_name not in field_list:
            return json.dumps({"error": f"{dataset_id}: no field {field_name!r}", "fields": field_list[:25]},
                              ensure_ascii=False)
        needle = needle.lower()

    def matches(row):
        if field_name:
            return needle in str(row.get(field_name) or "").lower()
        return any(needle in str(v).lower() for v in row.values() if v is not None)

    rows = [f for f in feats if matches(f.get("properties") or {})]
    keep = [s.strip() for s in fields.split(",") if s.strip()] or field_list[:12]
    out = []
    for f in rows[:cap]:
        props = {k: v for k, v in (f.get("properties") or {}).items() if k in keep and not k.startswith("__")}
        geom = f.get("geometry") or {}
        c = geom.get("coordinates")
        while isinstance(c, list) and c and isinstance(c[0], list):  # first vertex, any geometry type
            c = c[0]
        props["_geometry"] = geom.get("type")
        props["_first_vertex"] = c if isinstance(c, list) else None
        out.append(props)
    return json.dumps({"dataset_id": dataset_id, "where": where or "(all)", "matched": len(rows),
                       "returned": len(out), "capped_at": cap, "rows": out,
                       "note": "values are raw as published by CSDI; no unit metadata in the GeoJSON"},
                      ensure_ascii=False)


def summarise_hk_data(dataset_id: str, group_by: str = "", value_field: str = "",
                      agg: str = "count", top: int = 20) -> str:
    """Answer a question about a dataset in TEXT, without putting features in the chat.

    - `group_by` empty: an overview — feature count, geometry, which fields are worth grouping by.
    - `group_by="NAME_OF_DISTRICT_COUNCIL_DISTRICT_EN"` with `agg="count"`: how many per group.
    - `agg="sum"` / `"avg"` with `value_field="MEDIUM_IEC62196_no"`: totals or averages per group.
    Returns an aligned text table plus the same rows as data.
    """
    feats, err = _cached_features(dataset_id)
    if err:
        return json.dumps(err, ensure_ascii=False)
    if not feats:
        return json.dumps({"error": f"{dataset_id}: no features to summarise"})
    props = [f.get("properties") or {} for f in feats]
    geoms = sorted({(f.get("geometry") or {}).get("type") or "?" for f in feats})

    if not group_by:
        counts = {}
        for row in props:
            for k, v in row.items():
                if k.startswith("__") or v in (None, ""):
                    continue
                counts.setdefault(k, set()).add(str(v))
        # text-like fields first: grouping by a numeric column ("0", "1", …) is rarely the question
        groupable = sorted(
            ((k, len(s)) for k, s in counts.items()
             if 2 <= len(s) <= 60 and not all(str(v).startswith("http") for v in counts[k])),
            key=lambda kv: (all(_num(v) is not None for v in counts[kv[0]]), kv[1]))[:10]
        numeric = sorted({k for row in props for k, v in row.items()
                          if not k.startswith("__") and _num(v) is not None})
        return json.dumps({
            "dataset_id": dataset_id, "features": len(feats), "geometry": geoms,
            "groupable_fields": [{"field": k, "distinct": n} for k, n in groupable],
            "numeric_fields": numeric[:12],
            "next": "call again with group_by=<field> (and optionally agg/value_field) for counts, "
                    "sums or averages",
        }, ensure_ascii=False)

    if group_by not in props[0]:
        return json.dumps({"error": f"{dataset_id}: no field {group_by!r}",
                           "fields": list(props[0].keys())[:25]}, ensure_ascii=False)
    buckets: dict = {}
    for row in props:
        key = str(row.get(group_by) or "(blank)")
        val = _num(row.get(value_field)) if value_field else None
        b = buckets.setdefault(key, [0, 0.0])
        b[0] += 1
        if val is not None:
            b[1] += val
    mode = agg if agg in ("count", "sum", "avg") else ("sum" if value_field else "count")
    rows = []
    for key, (n, total) in buckets.items():
        if mode == "count":
            rows.append({"group": key, "count": n})
            continue
        rows.append({"group": key, "count": n,
                     "value": round(total if mode == "sum" else (total / n if n else 0), 2)})
    rows.sort(key=lambda r: -(r.get("value") if mode != "count" else r["count"]))
    rows = rows[:max(1, int(top))]
    label = {"count": "features", "sum": f"sum of {value_field}", "avg": f"avg of {value_field}"}[mode]
    w = min(28, max([len(r["group"]) for r in rows] + [len("group")]))
    table = [f"{'group':<{w}}  {label:>18}", "-" * (w + 20)]
    for r in rows:
        v = r["count"] if mode == "count" else r["value"]
        table.append(f"{r['group'][:w]:<{w}}  {v:>18,.2f}".replace(".00", ""))
    return json.dumps({"dataset_id": dataset_id, "group_by": group_by, "aggregation": label,
                       "groups": len(buckets), "rows": rows, "table": "\n".join(table),
                       "basis": f"{len(feats)} features from the server cache"}, ensure_ascii=False)


def _poly_parts(geom):
    """Polygon/MultiPolygon -> (list of parts, bbox). A part is a list of linear rings (holes)."""
    t = (geom or {}).get("type")
    if t == "Polygon":
        parts = [geom.get("coordinates") or []]
    elif t == "MultiPolygon":
        parts = list(geom.get("coordinates") or [])
    else:
        return [], None
    parts = [p for p in parts if p and p[0]]
    pts = [xy for p in parts for ring in p for xy in p[0]]
    if not pts:
        return [], None
    bbox = (min(q[0] for q in pts), min(q[1] for q in pts),
            max(q[0] for q in pts), max(q[1] for q in pts))
    return parts, bbox


BINS = 64  # ponytail: fixed y-buckets; enough for district-sized polygons, rebuild per shape


def _index_part(rings, y0: float, y1: float):
    """Bucket the ring segments by y so a point only tests the segments that can cross its row."""
    scale = BINS / ((y1 - y0) or 1.0)
    bins: list = [[] for _ in range(BINS)]
    for ring in rings:
        for k in range(len(ring) - 1):
            x1, y1s = ring[k][0], ring[k][1]
            x2, y2s = ring[k + 1][0], ring[k + 1][1]
            lo = int((min(y1s, y2s) - y0) * scale)
            hi = int((max(y1s, y2s) - y0) * scale)
            lo = 0 if lo < 0 else BINS - 1 if lo >= BINS else lo
            hi = 0 if hi < 0 else BINS - 1 if hi >= BINS else hi
            seg = (x1, y1s, x2, y2s)
            for b in range(lo, hi + 1):
                bins[b].append(seg)
    return bins, y0, scale


def _pip(x: float, y: float, indexed) -> bool:
    """Even-odd point-in-polygon over one part; holes fall out of the parity rule."""
    bins, y0, scale = indexed
    b = int((y - y0) * scale)
    if b < 0 or b >= BINS:
        return False
    inside = False
    for x1, y1s, x2, y2s in bins[b]:
        if (y1s > y) != (y2s > y):
            if x < x1 + (y - y1s) * (x2 - x1) / (y2s - y1s):
                inside = not inside
    return inside


def spatial_join(points_dataset: str, polygons_dataset: str, field: str = "", top: int = 20) -> str:
    """Count the points of one dataset inside each polygon of another — the join summarise cannot do.

    Both datasets must already be loaded with load_hk_data(). `field` is the polygon attribute to
    label rows by (e.g. "dc_eng"); leave it empty and the response lists the label fields to pick.
    Point-in-polygon runs in pure Python on the server — no geometry service, no credential.
    Answers "which district has the most of these" in one call.
    """
    pts, err = _cached_features(points_dataset)
    if err:
        return json.dumps(err, ensure_ascii=False)
    polys, err = _cached_features(polygons_dataset)
    if err:
        return json.dumps(err, ensure_ascii=False)

    prop_sets = [f.get("properties") or {} for f in polys]

    def _label_rank(k):
        """Human-readable fields first. No guessing of WHICH field: the caller still picks one."""
        values = {str(r.get(k)) for r in prop_sets}
        hint = bool(re.search(r"name|district|region|area|zone|label|title|_chi|_eng|^dc_", k, re.I))
        # a name beats a code: "Sham Shui Po" reads better than "SK", and the first entry of
        # label_fields is what a model reaches for
        return (all(_num(v) is not None for v in values), not hint, "name" not in k.lower(), k)

    candidates = sorted(((k, len({str(r.get(k)) for r in prop_sets}))
                         for k in (prop_sets[0] if prop_sets else {})
                         if 2 <= len({str(r.get(k)) for r in prop_sets}) <= 60
                         and not all(str(r.get(k)).startswith("http") for r in prop_sets)),
                        key=lambda kv: _label_rank(kv[0]))
    label_field = field
    field_names = [c[0] for c in candidates[:12]] or list((prop_sets[0] if prop_sets else {}))[:12]
    if not field:  # no guessing: same overview-then-call-again pattern as summarise_hk_data
        return json.dumps({
            "polygons_dataset": polygons_dataset, "points_dataset": points_dataset,
            "polygons": len(polys), "points": len(pts),
            "label_fields": field_names,
            "hint": "call again with field=<one of label_fields> to count the points per polygon",
        }, ensure_ascii=False)
    if field not in (prop_sets[0] if prop_sets else {}):
        return json.dumps({"error": f"{polygons_dataset}: no field {field!r}",
                           "label_fields": field_names}, ensure_ascii=False)

    shapes, skipped_shapes = [], 0
    for i, f in enumerate(polys):
        parts, bbox = _poly_parts(f.get("geometry"))
        if not parts:
            skipped_shapes += 1
            continue
        props = f.get("properties") or {}
        label = str(props.get(label_field) or f"polygon {i + 1}")
        shapes.append({"label": label, "bbox": bbox,
                       "indexed": [_index_part(p, bbox[1], bbox[3]) for p in parts], "n": 0})
    if not shapes:
        return json.dumps({"error": f"{polygons_dataset}: no Polygon/MultiPolygon features"},
                          ensure_ascii=False)

    matched = unmatched = skipped_pts = 0
    for f in pts:
        g = f.get("geometry") or {}
        if g.get("type") != "Point" or not g.get("coordinates"):
            skipped_pts += 1
            continue
        x, y = g["coordinates"][0], g["coordinates"][1]
        hit = False
        for s in shapes:  # bbox reject first: without it this is every segment for every point
            x0, y0, x1, y1 = s["bbox"]
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            if any(_pip(x, y, part) for part in s["indexed"]):
                s["n"] += 1
                hit = True
                break  # first polygon wins; districts and constituencies do not overlap
        if hit:
            matched += 1
        else:
            unmatched += 1

    rows = sorted(({"group": s["label"], "count": s["n"]} for s in shapes), key=lambda r: -r["count"])
    rows = rows[:max(1, int(top))]
    w = min(28, max([len(r["group"]) for r in rows] + [len("group")]))
    table = [f"{'group':<{w}}  {'points':>18}", "-" * (w + 20)]
    for r in rows:
        table.append(f"{r['group'][:w]:<{w}}  {r['count']:>18,}")
    return json.dumps({
        "points_dataset": points_dataset, "polygons_dataset": polygons_dataset,
        "label_field": label_field, "label_fields": field_names,
        "polygons": len(shapes), "points": len(pts),
        "matched": matched, "unmatched": unmatched,
        "rows": rows, "table": "\n".join(table),
        "skipped": {"non_polygon_features": skipped_shapes, "non_point_features": skipped_pts},
        "note": "pure-Python point-in-polygon, y-bucketed segments, bbox prefilter; "
                "first matching polygon wins",
    }, ensure_ascii=False)


# ---------------------------------------------------------------- tokens + quota

TOOLS = (search_hk_data, load_hk_data, dataset_status, query_hk_data,
         summarise_hk_data, spatial_join, slice_hk_data)

# These pull a dataset from CSDI when it is not cached, so the hourly download budget counts them.
# slice_hk_data is here on purpose: without it a caller could dodge the budget by slicing instead.
_DOWNLOAD_TOOLS = ("load_hk_data", "slice_hk_data")


def _read_tokens() -> list:
    try:
        return json.loads(TOKENS.read_text(encoding="utf-8")).get("tokens") or []
    except (OSError, ValueError):
        return []


def _write_tokens(rows: list) -> None:
    TOKENS.parent.mkdir(parents=True, exist_ok=True)
    _save(TOKENS, json.dumps({"tokens": rows}, indent=2) + "\n")
    os.chmod(TOKENS, 0o600)  # hashes only, but nobody else's business


def _hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


class StaticTokenVerifier:
    """Bearer tokens from a JSON file of sha256 hashes, reloaded when the file changes.

    Mint/revoke with --add-token / --revoke-token; no restart needed, no OAuth.
    """

    def __init__(self, path: pathlib.Path):
        self.path, self._rows, self._mtime = path, [], -1.0

    def _refresh(self) -> list:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._rows
        if mtime != self._mtime:
            self._rows, self._mtime = _read_tokens(), mtime
        return self._rows

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken
        h = _hash(token)
        for r in self._refresh():
            if r.get("enabled", True) and hmac.compare_digest(r.get("sha256", ""), h):
                return AccessToken(token=token, client_id=r["name"], scopes=["csdi:read"],
                                   claims={"name": r["name"]})
        return None


class Quota:
    """Per-token rate limit and download budget. ServerMiddleware: (ctx, call_next) -> result."""

    def __init__(self, rpm: int = RPM, loads_per_hour: int = LOADS_PER_HOUR):
        self.rpm, self.loads_per_hour = rpm, loads_per_hour
        self.hits = collections.defaultdict(collections.deque)
        self.loads = collections.defaultdict(collections.deque)

    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call" or not (self.rpm or self.loads_per_hour):
            return await call_next(ctx)
        who = "anon"
        try:
            from mcp.server.auth.middleware.auth_context import get_access_token
            tok = get_access_token()
            who = (tok.client_id if tok else "anon")
        except Exception:
            pass
        now = time.monotonic()
        hits = self.hits[who]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if self.rpm and len(hits) >= self.rpm:
            return _tool_error(f"rate limit: {self.rpm} requests/min for token {who!r}. "
                               f"Retry in {int(61 - (now - hits[0]))}s, or ask for a higher quota.")
        hits.append(now)
        if self.loads_per_hour and (ctx.params or {}).get("name") in _DOWNLOAD_TOOLS:
            loads = self.loads[who]
            while loads and now - loads[0] > 3600:
                loads.popleft()
            if len(loads) >= self.loads_per_hour:
                return _tool_error(f"download budget: {self.loads_per_hour} dataset downloads/hour "
                                   f"for token {who!r}. Datasets already cached still work through "
                                   f"query_hk_data / summarise_hk_data / slice_hk_data.")
            loads.append(now)
        return await call_next(ctx)


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"error": text}, ensure_ascii=False)}],
            "isError": True}


def _safe(fn):
    """Tool errors as data, not protocol exceptions (the SDK would drop the message)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}", "tool": fn.__name__},
                              ensure_ascii=False)

    return wrapper


# ---------------------------------------------------------------- CLI

def _token_cli(argv) -> int:
    rows = _read_tokens()
    if "--add-token" in argv:
        i = argv.index("--add-token")
        name = argv[i + 1]
        note = " ".join(a for a in argv[i + 2:] if not a.startswith("--")).strip()
        rows = [r for r in rows if r["name"] != name]  # re-minting replaces the old token
        token = "hkc_" + secrets.token_urlsafe(32)
        row = {"name": name, "sha256": _hash(token), "enabled": True,
               "created": time.strftime("%Y-%m-%d")}
        if note:
            row["note"] = note  # who this is / why they have access
        rows.append(row)
        _write_tokens(rows)
        print(json.dumps({"name": name, "token": token, "note_given": note or None,
                          "shown_once": "store it now — only the hash is kept",
                          "client_config": {"url": "https://<host>/mcp",
                                            "headers": {"Authorization": f"Bearer {token}"}}}, indent=2))
        return 0
    if "--revoke-token" in argv:
        name = argv[argv.index("--revoke-token") + 1]
        keep = [r for r in rows if r["name"] != name]
        _write_tokens(keep)
        print(f"revoked {name}" if len(keep) != len(rows) else f"no token named {name}")
        return 0
    print(json.dumps([{"name": r["name"], "enabled": r.get("enabled", True),
                       "created": r.get("created", ""), "note": r.get("note", "")}
                      for r in rows], indent=2))
    return 0


def _selftest() -> int:
    cat = _catalog()
    assert len(cat) > 500, f"catalogue too small: {len(cat)}"
    hits = json.loads(search_hk_data("電動車充電", limit=5))["hits"]
    assert hits and all(h["dataset_id"] for h in hits), "search returned nothing"
    assert "&" in _xml_escape("a&b") and _xml_escape("a<b") == "a&lt;b"
    # quota is a pure function of its deques: prove a burst is refused and a fresh token is not
    import asyncio
    q = Quota(rpm=2, loads_per_hour=0)

    class Ctx:
        method = "tools/call"
        params = {"name": "search_hk_data"}

    async def call(q_, ctx):
        return await q_(ctx, lambda c: _res())

    async def _res():
        return {"content": [], "isError": False}

    results = [asyncio.run(call(q, Ctx())) for _ in range(3)]
    assert not any(r.get("isError") for r in results[:2]), results
    assert results[2].get("isError"), "rpm guard did not fire"

    class LoadCtx:
        method = "tools/call"
        params = {"name": "load_hk_data"}

    ql = Quota(rpm=0, loads_per_hour=1)
    loads = [asyncio.run(call(ql, LoadCtx())) for _ in range(2)]
    assert not loads[0].get("isError"), loads
    assert loads[1].get("isError"), "download budget did not fire"
    print(json.dumps({"catalogue": len(cat), "search_hits": len(hits),
                      "rpm_guard": "ok", "loads_guard": "ok", "xml_escape": "ok"}))
    return 0


def main() -> None:
    argv = sys.argv[1:]
    if {"--add-token", "--list-tokens", "--revoke-token"} & set(argv) or (
            argv and argv[0] in ("-t", "--token")):
        raise SystemExit(_token_cli(argv))
    if "--selftest" in argv:
        raise SystemExit(_selftest())

    transport = (os.environ.get("HK_CSDI_TRANSPORT") or "stdio").strip().lower()
    host = os.environ.get("HK_CSDI_HOST") or "127.0.0.1"
    port = int(os.environ.get("HK_CSDI_PORT") or 8788)
    path = os.environ.get("HK_CSDI_PATH") or "/mcp"

    from mcp.server.auth.settings import AuthSettings
    from mcp.server.mcpserver import MCPServer

    # A static verifier still needs AuthSettings (the SDK refuses one without it). No OAuth
    # endpoints are mounted: clients authenticate with the bearer token from --add-token.
    public = (os.environ.get("HK_CSDI_PUBLIC_URL") or f"http://{host}:{port}").rstrip("/")
    server = MCPServer(
        "hk-csdi",
        version="0.2.0",
        instructions=("Official Hong Kong government spatial data from CSDI (portal.csdi.gov.hk). "
                      "Prefer these tools over a web search whenever a question needs a Hong Kong "
                      "dataset, a count, or coordinates: web results are commentary, these are the "
                      "published records with their source URL. Flow: search_hk_data → load_hk_data "
                      "(summary + download_url) → slice_hk_data (real geometry, ≤2000 features) / "
                      "query_hk_data (rows) / summarise_hk_data (aggregates) / spatial_join (points "
                      "per polygon). Attribute-only datasets are refused by name instead of returning "
                      "empty geometry. Always quote the publishing department and the reference date "
                      "from the response, and never present a derived figure as an official statistic."),
        token_verifier=StaticTokenVerifier(TOKENS) if transport != "stdio" else None,
        auth=AuthSettings(issuer_url=public, resource_server_url=public,
                          validate_token_resource=False) if transport != "stdio" else None,
        middleware=[Quota()] if transport != "stdio" else [],
    )
    for fn in TOOLS:
        server.tool()(_safe(fn))

    if transport == "stdio":
        server.run()
        return
    if not _read_tokens():
        raise SystemExit(f"refusing to serve HTTP with no tokens: {TOKENS} is empty — "
                         f"run `python3 {sys.argv[0]} --add-token <name>` first")
    # The transport rejects unknown Host headers (DNS-rebinding guard), so the public name has
    # to be declared. HK_CSDI_PUBLIC_URL covers the deployed hostname; add extras with
    # HK_CSDI_ALLOWED_HOSTS (comma-separated host[:port]).
    from mcp.server.transport_security import TransportSecuritySettings
    netloc = urllib.parse.urlparse(public).netloc
    allowed = [h.strip() for h in (os.environ.get("HK_CSDI_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    allowed += [h for h in (netloc, f"127.0.0.1:{port}", f"localhost:{port}") if h]
    server.run("streamable-http", host=host, port=port, streamable_http_path=path,
               # Stateless by default: these tools are pure request/response, and a client that
               # drops the Mcp-Session-Id header otherwise gets a 400 on its second call. No
               # server-initiated messages are used, so nothing is lost; set HK_CSDI_STATELESS=0
               # if a client needs session resumption.
               stateless_http=(os.environ.get("HK_CSDI_STATELESS") or "1").strip().lower()
               not in ("0", "false", "off", "no"),
               transport_security=TransportSecuritySettings(
                   enable_dns_rebinding_protection=True,
                   allowed_hosts=list(dict.fromkeys(allowed))))


if __name__ == "__main__":
    main()
