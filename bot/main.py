"""
bot/main.py — single entry point.
Starts everything in one process:
  1. WandB poller (background thread)
  2. Admin UI / FastAPI server (background thread)
  3. Discord bot (main async loop)
"""

from __future__ import annotations

import asyncio
import logging
import threading

import discord
import uvicorn
from dotenv import load_dotenv

load_dotenv()

from config.settings import (
    DISCORD_TOKEN,
    WANDB_ENTITY, WANDB_PROJECT,
    WANDB_POLL_INTERVAL, WANDB_TARGET_RECORDS, WANDB_MAX_RUNS, WANDB_API_TIMEOUT,
    ADMIN_PORT,
)
from bot.discord_chat import DiscordChatClient
from data.wandb_service import WandBPoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
bot = DiscordChatClient(intents=intents)


def _start_admin_ui():
    uvicorn.run(
        "admin_ui.server:app",
        host="0.0.0.0",
        port=ADMIN_PORT,
        log_level="warning",
    )


async def main():
    # 1. WandB poller
    if WANDB_ENTITY and WANDB_PROJECT:
        poller = WandBPoller(
            entity=WANDB_ENTITY,
            project=WANDB_PROJECT,
            interval=WANDB_POLL_INTERVAL,
            target_records=WANDB_TARGET_RECORDS,
            max_runs=WANDB_MAX_RUNS,
            api_timeout=WANDB_API_TIMEOUT,
        )
        poller.start()
    else:
        logger.warning("WANDB_ENTITY or WANDB_PROJECT not set — wandb poller disabled")

    # 2. Admin UI
    threading.Thread(target=_start_admin_ui, daemon=True, name="admin-ui").start()
    logger.info(f"Admin UI running on http://0.0.0.0:{ADMIN_PORT}")

    # 3. Discord bot
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
