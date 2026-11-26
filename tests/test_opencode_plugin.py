"""The OpenCode plugin must know every CLI group, and must not invent one.

`plugins/opencode/index.ts` names the groups in its tool description and answers a typo from that
list, so a group added to `um/cli.py` without touching the plugin would leave the description stale.
This test is the sync check (it is also the "test with tool changes" CONTRIBUTING asks for).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from um.cli import GROUPS

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "opencode" / "index.ts"

# rows of the `GROUPS` array: { name: "scan", blurb: "..." },
_ROW = re.compile(r'\{\s*name:\s*"([a-z0-9]+)"')


def plugin_groups() -> list[str]:
    source = PLUGIN.read_text(encoding="utf-8")
    start = source.index("const GROUPS = [")
    end = source.index("\n]", start)  # the array's closing line, so a "]" inside a blurb can't cut it short
    return _ROW.findall(source[start:end])


def test_plugin_lists_every_cli_group():
    assert plugin_groups() == GROUPS


def test_plugin_file_exists():
    assert PLUGIN.is_file()


def test_plugin_does_not_use_config_substitution():
    # OpenCode expands {env:VAR} only in config files; a server a plugin sets would get the literal text.
    assert "Bearer {env:" not in PLUGIN.read_text(encoding="utf-8")


# --- MCP servers the plugin registers -------------------------------------------------------------------
# The plugin is loaded under Node (type stripping, 22.18+) against a stub `@opencode/plugin`, with a fake
# `ctx.mcp` that records what `setup` sets. Skipped where Node can't run TypeScript directly.

_HARNESS = r"""
import plugin from "./index.ts"
const existing = JSON.parse(process.env.UM_EXISTING || "{}")
const servers = { ...existing }
const ctx = {
  location: { directory: process.cwd() },
  options: JSON.parse(process.env.UM_OPTIONS || "{}"),
  mcp: { transform: async (fn) => fn({ get: (n) => servers[n], set: (n, v) => { servers[n] = v } }) },
  tool: { transform: async () => {} },
}
await plugin.setup(ctx)
process.stdout.write(JSON.stringify(servers))
"""


def _node_strips_types() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    probe = subprocess.run([node, "--input-type=module-typescript", "-e", "const a: number = 1"],
                           capture_output=True, text=True, check=False)
    return probe.returncode == 0


needs_node = pytest.mark.skipif(not _node_strips_types(), reason="needs Node 22.18+ (TypeScript type stripping)")


def registered(tmp_path, env: dict[str, str], options: dict | None = None, existing: dict | None = None) -> dict:
    stub = tmp_path / "node_modules" / "@opencode" / "plugin"
    stub.mkdir(parents=True, exist_ok=True)
    (stub / "package.json").write_text('{"name": "@opencode/plugin", "type": "module", "main": "index.js"}')
    (stub / "index.js").write_text("export const Plugin = { define: (p) => p }\n")
    (tmp_path / "package.json").write_text('{"type": "module"}')
    shutil.copy(PLUGIN, tmp_path / "index.ts")
    (tmp_path / "harness.mjs").write_text(_HARNESS)
    clean = {k: v for k, v in os.environ.items() if k not in ("FAL_KEY", "AIMLAPI_API_KEY")}
    clean.update(env, UM_OPTIONS=json.dumps(options or {}), UM_EXISTING=json.dumps(existing or {}))
    out = subprocess.run(["node", "--no-warnings", "harness.mjs"], cwd=tmp_path, env=clean,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


KEY = "test-aimlapi-key"


@needs_node
def test_aimlapi_mcp_registered_but_disabled(tmp_path):
    server = registered(tmp_path, {"AIMLAPI_API_KEY": KEY})["aimlapi"]
    assert server["disabled"] is True
    assert server["type"] == "remote"
    assert server["oauth"] is False
    # the key goes to the exact host only, over https, under the exact path
    assert server["url"] == "https://mcp.aimlapi.com/mcp"
    assert server["headers"] == {"Authorization": f"Bearer {KEY}"}


@needs_node
def test_aimlapi_mcp_turned_on_by_option(tmp_path):
    assert registered(tmp_path, {"AIMLAPI_API_KEY": KEY}, {"aimlapi": True})["aimlapi"]["disabled"] is False
    # anything but a literal true leaves it off
    assert registered(tmp_path, {"AIMLAPI_API_KEY": KEY}, {"aimlapi": "yes"})["aimlapi"]["disabled"] is True


@needs_node
def test_no_aimlapi_key_means_no_aimlapi_server(tmp_path):
    servers = registered(tmp_path, {"FAL_KEY": "test-fal-key"})
    assert "aimlapi" not in servers
    # the fal key never reaches the AI/ML API host, and fal is registered exactly as before
    assert servers["fal"]["url"] == "https://mcp.fal.ai/mcp"
    assert all("aimlapi.com" not in s["url"] for s in servers.values() if s["headers"]["Authorization"].endswith("test-fal-key"))


@needs_node
def test_aimlapi_key_never_sent_to_other_servers(tmp_path):
    servers = registered(tmp_path, {"AIMLAPI_API_KEY": KEY, "FAL_KEY": "test-fal-key"})
    with_key = [s["url"] for s in servers.values() if KEY in json.dumps(s.get("headers", {}))]
    assert with_key == ["https://mcp.aimlapi.com/mcp"]
    assert servers["fal"]["headers"] == {"Authorization": "Bearer test-fal-key"}


@needs_node
def test_user_aimlapi_entry_is_kept(tmp_path):
    mine = {"type": "remote", "url": "https://example.test/mcp", "disabled": False}
    assert registered(tmp_path, {"AIMLAPI_API_KEY": KEY}, existing={"aimlapi": mine})["aimlapi"] == mine


def test_aimlapi_key_not_written_in_plugin_source():
    source = PLUGIN.read_text(encoding="utf-8")
    assert 'url: AIMLAPI_MCP_URL' in source
    assert 'const AIMLAPI_MCP_URL = "https://mcp.aimlapi.com/mcp"' in source
    assert "process.env.AIMLAPI_API_KEY" in source
