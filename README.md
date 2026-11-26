# hk-csdi-mcp

An MCP server for **Hong Kong CSDI** spatial data: search 1,166 official datasets in English or
Chinese, download one as GeoJSON, read rows, aggregate by any field, and count points inside
polygons. One file, six tools, `mcp` as the only dependency.

Runs two ways from the same code:

- **locally over stdio** — paste it into Claude Desktop, Claude Code, Cursor, VS Code, CodeBuddy;
- **remotely over streamable HTTP** — one bearer token per person, revocable, rate-limited.

```
tools   6        runtime deps   1 (mcp)        datasets   1,166 (live CSDI catalogue)
```

The data is not bundled: every call reads `portal.csdi.gov.hk` and caches the response locally, so
the answers are the published ones, not a snapshot that rots in the repo.

## Tools

| tool | what it answers |
|---|---|
| `search_hk_data` | "what CSDI datasets are about 電動車充電?" → ranked `dataset_id` + bilingual title. Chinese and English both work; HK usage is expanded (廁所 → 洗手間, 行人路 → pedestrian). |
| `dataset_status` | is this dataset still served, and how stale is the record? `probe=true` reads 2 KB of the export endpoint to confirm it answers right now. |
| `load_hk_data` | download one dataset as WGS84 GeoJSON into the cache; returns a **summary** (feature count, geometry types, fields, bbox, `cached_days`) — never the features. |
| `query_hk_data` | a few individual rows (`where="FIELD=value"` or free text), capped at 20, so a single value can be checked. |
| `summarise_hk_data` | counts / sums / averages per group, as an aligned text table plus the same rows as data. Group by district, call it once to discover which fields are worth grouping. |
| `spatial_join` | points-in-polygons in pure Python — "which district has the most of these" — no geometry service, no credential. |

The catalogue and the GeoJSON exports are the only things downloaded; nothing is written outside
`HK_CSDI_DIR`.

## Local use (stdio, no token)

```json
{
  "mcpServers": {
    "hk-csdi": {
      "command": "python3",
      "args": ["/absolute/path/to/hk_csdi_mcp.py"]
    }
  }
}
```

Or install it as a package and let a client launch it:

```bash
pip install "git+https://github.com/Cyruschu430/hk-csdi-mcp"
hk-csdi-mcp                     # speaks MCP over stdio
hk-csdi-mcp --selftest          # catalogue + search + quota guards, prints one JSON line
```

## Remote use (streamable HTTP + bearer tokens)

Anyone running a public MCP endpoint needs to answer two questions before they hand out a URL:
*who may call it*, and *how does a token get revoked*. Both are one file here.

```bash
export HK_CSDI_TRANSPORT=streamable-http
export HK_CSDI_PORT=8788
export HK_CSDI_TOKENS=/var/lib/hk-csdi-mcp/tokens.json
export HK_CSDI_PUBLIC_URL=https://mcp.example.com   # advertised as the resource identifier

python3 hk_csdi_mcp.py --add-token alice    # prints the token ONCE; only its sha256 is stored
python3 hk_csdi_mcp.py --list-tokens
python3 hk_csdi_mcp.py --revoke-token alice # effective on the next request, no restart
python3 hk_csdi_mcp.py                      # serve
```

The token file is re-read when its mtime changes, so minting and revoking never need a restart.
Comparison is `hmac.compare_digest` over sha256 hashes — the plaintext never touches disk.

Clients that accept custom headers (Claude Code, Cursor, VS Code, `mcp-remote`) connect with:

```json
{
  "mcpServers": {
    "hk-csdi": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer hkc_..." }
    }
  }
}
```

Claude Code:

```bash
claude mcp add --transport http hk-csdi https://mcp.example.com/mcp \
  --header "Authorization: Bearer hkc_..."
```

`claude.ai` web connectors and ChatGPT connectors require OAuth, which this server does not
implement — use a client that sends headers, or run it locally over stdio.

### Abuse control

| env | default | effect |
|---|---|---|
| `HK_CSDI_RPM` | 60 | `tools/call` per minute per token, then a tool-level error naming the retry delay |
| `HK_CSDI_LOADS_PER_HOUR` | 30 | `load_hk_data` downloads per token per hour; already-cached datasets stay readable |
| `HK_CSDI_STALE_DAYS` | 30 | a cached dataset older than this is re-downloaded before it is quoted as current |

A refusal comes back as a normal tool result with `isError`, so the calling model reads the reason
and stops, instead of retrying a protocol exception.

Bind to `127.0.0.1` and terminate TLS in front (Caddy/nginx). The server sets no session limit of
its own beyond the SDK defaults; one process serves every token from one cache directory, so a
dataset downloaded by one caller is warm for the next. `deploy/` carries a systemd unit and a
Caddy site block that already has streaming and long timeouts configured for MCP.

## Tests

```bash
python3 test_auth.py         # no token → 401, wrong token → 401, 6 tools, quota bites, revoke is live
python3 test_mcp_client.py   # real stdio handshake, tool set, search + load + status (needs network)
python3 hk_csdi_mcp.py --selftest
```

`test_auth.py` needs no network: it spawns the server on a free port and only exercises auth,
the quota middleware and the tool registry.

## Data and attribution

All datasets come from the **Common Spatial Data Infrastructure** portal operated by the
Development Bureau, Government of the HKSAR — <https://portal.csdi.gov.hk>. Ownership and licence of
each dataset stay with its publishing department; cite the department named in the dataset record
(`creator`) when you quote a number. `dataset_status` reports CSDI's `dct:modified` (record revision
date), which is not the data's reference period.

Search ranks locally over the harvested catalogue; when the local index finds nothing the query is
forwarded to the CSDI CSW endpoint, and the response marks those hits `source: "csw"`.

Not included on purpose: map rendering, static image export, charts, ArcGIS Online publishing and
BIM/3D. This server is the data path only.

## Licence

MIT © 2026 Cyrus Chu. Built by [Cyrus Chu](https://cyruschu.com) · [LinkedIn](https://www.linkedin.com/in/cyruschu/).
