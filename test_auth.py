#!/usr/bin/env python3
"""HTTP auth test: no token / wrong token are refused, a good token works, the quota bites.

    python3 test_auth.py        # spawns the server on a free port, no network needed

Pins the hardening that makes a public MCP endpoint safe: every tools/call needs a minted
bearer token, and the per-token budget answers as a tool error instead of doing the work.
"""
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

import httpx2

ROOT = pathlib.Path(__file__).resolve().parent
SERVER = ROOT / "hk_csdi_mcp.py"
TOOL_COUNT = 7


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mint(env: dict, name: str, note: str = "") -> str:
    argv = [sys.executable, str(SERVER), "--add-token", name] + ([note] if note else [])
    out = subprocess.run(argv, env=env, capture_output=True, text=True, check=True).stdout
    register = json.loads(subprocess.run([sys.executable, str(SERVER), "--list-tokens"], env=env,
                                         capture_output=True, text=True, check=True).stdout)
    assert register and register[0]["name"] == name, register
    assert register[0]["note"] == note, register  # the access register keeps who/why
    return json.loads(out)["token"]


async def rpc(client: httpx2.AsyncClient, url: str, method: str, params: dict,
              session: str | None = None, extra: dict | None = None):
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    if session:
        headers["Mcp-Session-Id"] = session
    headers.update(extra or {})
    r = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          headers=headers)
    if r.status_code != 200:
        return r, None
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        body = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data: ")]
        return r, body[-1] if body else None
    return r, r.json()


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="hk-csdi-auth-")
    env = dict(os.environ, HK_CSDI_TOKENS=f"{tmp}/tokens.json", HK_CSDI_DIR=f"{tmp}/work",
               HK_CSDI_TRANSPORT="streamable-http", HK_CSDI_RPM="2",
               HK_CSDI_LOADS_PER_HOUR="0")
    token = mint(env, "tester", "ci robot")
    port = free_port()
    env["HK_CSDI_PORT"] = str(port)
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("server did not open its port:\n" + (proc.stdout.read() if proc.stdout else ""))

        async with httpx2.AsyncClient(timeout=20) as c:
            anon, _ = await rpc(c, url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                       "clientInfo": {"name": "t", "version": "0"}})
            assert anon.status_code == 401, f"anonymous request answered {anon.status_code}"

            bad, _ = await rpc(c, url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                      "clientInfo": {"name": "t", "version": "0"}},
                               extra={"Authorization": "Bearer hkc_wrong"})
            assert bad.status_code == 401, f"bad token answered {bad.status_code}"

            auth = {"Authorization": f"Bearer {token}"}
            init, _ = await rpc(c, url, "initialize", {"protocolVersion": "2025-06-18",
                                                       "capabilities": {},
                                                       "clientInfo": {"name": "t", "version": "0"}},
                                extra=auth)
            assert init.status_code == 200, init.text
            session = init.headers.get("mcp-session-id")
            assert session, "no Mcp-Session-Id issued"

            listed, payload = await rpc(c, url, "tools/list", {}, session=session, extra=auth)
            names = {t["name"] for t in payload["result"]["tools"]}
            assert len(names) == TOOL_COUNT, names

            # RPM=2: two calls pass the quota, the third must be refused instead of executed
            results = []
            for _ in range(3):
                _, body = await rpc(c, url, "tools/call",
                                    {"name": "query_hk_data",
                                     "arguments": {"dataset_id": "no_such_dataset"}},
                                    session=session, extra=auth)
                results.append(body["result"])
            assert results[0].get("isError") is False, results[0]
            assert results[2].get("isError") is True, results[2]
            assert "rate limit" in results[2]["content"][0]["text"], results[2]

            # revocation is live: drop the token, next call is 401 without a restart
            subprocess.run([sys.executable, str(SERVER), "--revoke-token", "tester"], env=env, check=True,
                           capture_output=True)
            dead, _ = await rpc(c, url, "tools/list", {}, session=session, extra=auth)
            assert dead.status_code == 401, f"revoked token still answered {dead.status_code}"

        print(json.dumps({"anon": 401, "bad_token": 401, "tools": len(names),
                          "rpm_guard_third_call": "isError", "revoked": 401, "status": "ok"}))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
