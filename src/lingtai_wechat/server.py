"""LingTai WeChat MCP server.

Exposes a single omnibus ``wechat`` MCP tool that dispatches to
WechatManager for all 8 actions (send, check, read, reply, search,
contacts, add_contact, remove_contact). Inbound WeChat events flow into
the host agent's inbox via LICC.

Configuration:
    LINGTAI_WECHAT_CONFIG  — path to ``config.json``. ``credentials.json``
                             is read from the same directory and is written
                             by the ``lingtai-wechat-bootstrap`` flow
                             (recommended) or the headless ``cli_login``
                             fallback. See README.

Config schemas (plaintext, no env-indirection):

    config.json:
        {
          "cdn_base_url": "...",         // optional
          "poll_interval": 1.0,          // optional
          "allowed_users": ["wxid_..."]  // optional allow-list
        }

    credentials.json (written by lingtai-wechat-bootstrap or cli_login):
        {
          "bot_token": "...",
          "user_id": "wxid_...",
          "base_url": "..."
        }

Env vars injected by the LingTai kernel for LICC:
    LINGTAI_AGENT_DIR — host agent's working directory.
    LINGTAI_MCP_NAME  — this MCP's registry name (typically "wechat").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import api
from .licc import push_inbox_event
from .manager import WechatManager, SCHEMA, DESCRIPTION

log = logging.getLogger("lingtai_wechat")


_SERVER_INSTRUCTIONS = (
    "lingtai-wechat: WeChat client via iLink Bot API. "
    "Configure via the LINGTAI_WECHAT_CONFIG env var pointing at config.json "
    "(credentials.json must live in the same directory; produced by the "
    "QR-code login flow). "
    "Inbound messages flow into the host agent's inbox via LICC. "
    "Setup, config schema, and troubleshooting: "
    "https://github.com/Lingtai-AI/lingtai-wechat"
)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config_and_credentials() -> tuple[dict, dict, Path]:
    """Read config.json + sibling credentials.json. Returns (config, creds, config_dir)."""
    config_path_raw = os.environ.get("LINGTAI_WECHAT_CONFIG")
    if not config_path_raw:
        raise ValueError(
            "LINGTAI_WECHAT_CONFIG env var not set — point it at your "
            "WeChat config.json file"
        )
    config_path = Path(config_path_raw).expanduser()
    if not config_path.is_absolute():
        base = Path(os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
        config_path = base / config_path
    if not config_path.is_file():
        raise FileNotFoundError(f"WeChat config not found: {config_path}")

    file_cfg = json.loads(config_path.read_text(encoding="utf-8"))

    creds_path = config_path.parent / "credentials.json"
    if not creds_path.is_file():
        raise FileNotFoundError(
            f"WeChat credentials not found: {creds_path}. "
            f"Run the bootstrap flow first to authenticate via QR code:\n"
            f"  lingtai-wechat-bootstrap {config_path.parent}\n"
            f"or, for a headless host:\n"
            f'  python -c "from lingtai_wechat.login import cli_login; '
            f"cli_login('{config_path.parent}')\""
        )
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    return file_cfg, creds, config_path.parent


# ---------------------------------------------------------------------------
# Manager construction
# ---------------------------------------------------------------------------

def build_manager() -> tuple[WechatManager, Path]:
    """Construct manager from env + config.json + credentials.json."""
    file_cfg, creds, _config_dir = load_config_and_credentials()

    bot_token = creds.get("bot_token")
    user_id = creds.get("user_id")
    if not bot_token:
        raise ValueError(
            "credentials.json missing 'bot_token'. Re-run the QR login flow."
        )
    if not user_id:
        raise ValueError(
            "credentials.json missing 'user_id'. Re-run the QR login flow."
        )

    base_url = creds.get("base_url") or file_cfg.get("base_url", api.DEFAULT_BASE_URL)
    cdn_base_url = file_cfg.get("cdn_base_url", api.CDN_BASE_URL)
    poll_interval = float(file_cfg.get("poll_interval", 1.0))
    allowed_users = file_cfg.get("allowed_users") or None

    agent_dir_raw = os.environ.get("LINGTAI_AGENT_DIR")
    working_dir = Path(agent_dir_raw) if agent_dir_raw else Path.cwd()
    working_dir.mkdir(parents=True, exist_ok=True)

    def _on_inbound(event: dict) -> None:
        push_inbox_event(
            sender=event["from"],
            subject=event["subject"],
            body=event["body"],
            metadata=event.get("metadata"),
            wake=event.get("wake", True),
        )

    mgr = WechatManager(
        base_url=base_url,
        cdn_base_url=cdn_base_url,
        token=bot_token,
        user_id=user_id,
        poll_interval=poll_interval,
        allowed_users=allowed_users,
        working_dir=working_dir,
        on_inbound=_on_inbound,
    )
    return mgr, working_dir


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

def build_server(
    manager: WechatManager | None,
    *,
    startup_error: str | None = None,
    startup_error_type: str | None = None,
) -> Server:
    server: Server = Server("lingtai-wechat", instructions=_SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="wechat",
                description=DESCRIPTION,
                inputSchema=SCHEMA,
            ),
        ]

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent]:
        if name != "wechat":
            raise ValueError(f"unknown tool: {name!r}")
        if manager is None:
            # Surface the actual startup exception type + message so the
            # agent/operator sees concrete remediation (e.g. PollerLockBusy
            # with ps/kill hints) instead of just "check stderr". stderr is
            # not always visible at the moment a tool call returns.
            result = {
                "status": "error",
                "error": (
                    "WeChat manager not initialized — server boot failed. "
                    "Run lingtai-wechat-bootstrap first if you haven't set "
                    "up credentials, or check the startup_error fields "
                    "below for the underlying exception."
                ),
                "startup_error_type": startup_error_type,
                "startup_error": startup_error,
            }
        else:
            try:
                result = await asyncio.to_thread(manager.handle, arguments)
            except Exception as e:
                result = {
                    "status": "error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
        return [types.TextContent(
            type="text", text=json.dumps(result, ensure_ascii=False),
        )]

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def serve() -> None:
    """Run the MCP server over stdio. Eagerly starts the iLink long-poll
    so inbound messages flow before the host expects them."""
    manager: WechatManager | None = None
    started = False
    startup_error: str | None = None
    startup_error_type: str | None = None
    try:
        manager, _wd = build_manager()
        manager.start()
        started = True
        log.info("WeChat listener running")
    except Exception as e:
        log.error(
            "eager start failed; tool calls will return errors until fixed: %s", e,
        )
        manager = None
        startup_error = str(e)
        startup_error_type = type(e).__name__

    server = build_server(
        manager,
        startup_error=startup_error,
        startup_error_type=startup_error_type,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if manager is not None and started:
            try:
                manager.stop()
            except Exception:
                pass
