"""
Discord message chat client.

Responds only to direct messages, direct mentions, and replies to the bot.
No slash commands and no text commands are registered.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord

from bot.codex_handler import ask_codex
from config.settings import (
    ALLOWED_CHANNEL_IDS,
    ANNOUNCEMENT_CHANNEL_IDS,
    ANNOUNCEMENT_POLL_INTERVAL,
    CHAIN_POLL_INTERVAL,
    TOP_MINER_ALERT_CHANNEL_IDS,
    TOP_MINER_ALERT_MENTION_EVERYONE,
    TOP_MINER_ALERTS_ENABLED,
    TOP_MINER_STARTUP_ALERT_ENABLED,
)
from data.fetcher import (
    build_live_data_block,
    get_all_uid_infos,
    get_uid_info,
    refresh_subnet_info,
)
from data.knowledge_store import get_announcements
from data.wandb_service import wandb_cache
from rag.pipeline import all_docs_context

logger = logging.getLogger(__name__)

MAX_DISCORD_MESSAGE = 1900

LIVE_DATA_TRIGGERS = {
    "price", "alpha", "liquidity", "pool", "uid", "rank",
    "score", "stake", "top", "emission", "tempo", "neuron",
    "subnet", "stats", "chain", "metagraph", "incentive",
    "hyperparameter", "hyperparameters", "kappa", "rho",
    "reg", "registration", "cost", "immunity", "difficulty",
    "commit", "reveal", "active", "activity", "serving", "registered",
    "cutoff", "yuma",
    "burn", "recycle", "recycled", "daily", "rate", "trust",
    "hotkey", "coldkey", "validator", "miner",
}

DOC_TRIGGERS = {
    "readme", "docs", "documentation", "setup", "install", "installation",
    "run", "launch", "validator", "miner", "architecture", "protocol",
    "scoring", "weight", "weights", "challenge", "llm", "endpoint",
    "troubleshoot", "readiness", "requirements",
    "btcli", "wallet", "coldkey", "hotkey", "register", "registration",
    "permit", "axon", "dendrite", "cuda", "gpu", "torch", "python",
    "venv", "pip", "docker", "firewall", "port", "error", "failed",
    "perturb", "unique", "vision", "future", "roadmap", "goal",
    "announcement", "announcements", "announce", "news", "update",
    "updates", "notice", "notices", "status",
    "potential", "opportunity", "interesting", "innovative", "compare",
    "repo", "repository", "codebase", "structure", "tree", "folder",
    "folders", "directory", "directories", "file", "files", "layout",
}


def _extract_uid(query: str) -> int | None:
    lowered = query.lower()
    patterns = [
        r"\buid\s*[:=#-]?\s*(\d+)\b",
        r"\b(?:miner|validator|neuron)\s+uid\s*[:=#-]?\s*(\d+)\b",
        r"\b(?:miner|validator|neuron)\s*[:=#-]\s*(\d+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return int(match.group(1))
    return None


def _tokens(query: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", query.lower()))


def _needs_live(query: str) -> bool:
    return bool(LIVE_DATA_TRIGGERS & _tokens(query))


def _needs_docs(query: str) -> bool:
    return bool(DOC_TRIGGERS & _tokens(query))


def _needs_all_uid_data(query: str) -> bool:
    tokens = _tokens(query)
    joined = " ".join(tokens)
    return (
        "all" in tokens
        and bool({"uid", "uids", "miner", "miners", "neuron", "neurons"} & tokens)
    ) or any(
        phrase in query.lower()
        for phrase in (
            "full leaderboard",
            "complete leaderboard",
            "all leaderboard",
            "all wandb",
            "all chain",
            "every uid",
            "every miner",
            "each uid",
            "each miner",
        )
    ) or "full leaderboard" in joined


def _strip_bot_mentions(content: str, bot_user: discord.ClientUser) -> str:
    mention_pattern = rf"<@!?{bot_user.id}>"
    return re.sub(mention_pattern, "", content).strip()


def _split_discord_message(text: str) -> list[str]:
    if len(text) <= MAX_DISCORD_MESSAGE:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        chunk = remaining[:MAX_DISCORD_MESSAGE]
        split_at = max(chunk.rfind("\n"), chunk.rfind(" "))
        if split_at < 500:
            split_at = MAX_DISCORD_MESSAGE
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return chunks


def _format_combined_uid(uid: int, chain: dict | None, wb: dict | None) -> str:
    lines = [f"UID {uid} combined chain + WandB data:"]
    if chain and "error" not in chain:
        lines.append(
            "  Chain: "
            + ", ".join(
                f"{key}={value}"
                for key, value in chain.items()
                if key != "uid"
            )
        )
    elif chain and "error" in chain:
        lines.append(f"  Chain: unavailable ({chain['error']})")
    else:
        lines.append("  Chain: unavailable")

    if wb:
        lines.append(
            "  WandB: "
            f"rank={wb['rank']}, avg_score={wb['avg_score']}, "
            f"last_score={wb['last_score']}, records={wb['records']}, "
            f"recent_scores={wb['recent_scores']}"
        )
    else:
        lines.append("  WandB: unavailable")
    return "\n".join(lines)


def _format_combined_top_miners() -> str:
    all_chain = get_all_uid_infos()
    if not all_chain or "error" in all_chain[0]:
        return wandb_cache.get_summary_block()

    wb_by_uid = {row["uid"]: row for row in wandb_cache.get_top_miners(1000)}
    top_chain = sorted(
        [row for row in all_chain if "error" not in row],
        key=lambda row: row.get("chain_rank", 10**9),
    )[:10]
    lines = [
        "Combined top UID data by on-chain incentive rank:",
        "Default order is chain_rank, derived from metagraph incentive. WandB fields are attached by matching UID when available.",
    ]
    for chain in top_chain:
        uid = chain["uid"]
        wb = wb_by_uid.get(uid)
        wb_part = (
            f"wandb_rank={wb['rank']}, avg_score={wb['avg_score']:.6f}, "
            f"last_score={wb['last_score']:.6f}, records={wb['records']}, "
            f"recent_scores={wb.get('recent_scores', [])}"
            if wb
            else "wandb_unavailable"
        )
        lines.append(
            f"  UID {uid}: "
            f"chain_rank={chain.get('chain_rank')}, "
            f"incentive={chain.get('chain_incentive')}, "
            f"stake={chain.get('stake')}, "
            f"trust={chain.get('trust', 'N/A')}, "
            f"hotkey={chain.get('hotkey')}, "
            f"coldkey={chain.get('coldkey')}; "
            f"{wb_part}"
        )
    return "\n".join(lines)


def _format_combined_all_uids() -> str:
    all_chain = get_all_uid_infos()
    if not all_chain or "error" in all_chain[0]:
        return wandb_cache.get_summary_block()

    wb_by_uid = {row["uid"]: row for row in wandb_cache.get_top_miners(1000)}
    ranked_chain = sorted(
        [row for row in all_chain if "error" not in row],
        key=lambda row: row.get("chain_rank", 10**9),
    )
    lines = [
        f"Combined all UID data by on-chain incentive rank ({len(ranked_chain)} chain UIDs):",
        "Default order is chain_rank, derived from metagraph incentive. WandB fields are attached by matching UID when available.",
        "Columns: uid | chain_rank | incentive | stake | trust | hotkey | coldkey | wandb_rank | avg_score | last_score | records | recent_scores",
    ]
    for chain in ranked_chain:
        uid = chain["uid"]
        wb = wb_by_uid.get(uid)
        wandb_rank = wb["rank"] if wb else "N/A"
        avg_score = f"{wb['avg_score']:.6f}" if wb else "N/A"
        last_score = f"{wb['last_score']:.6f}" if wb else "N/A"
        records = wb["records"] if wb else "N/A"
        recent_scores = wb.get("recent_scores", []) if wb else "N/A"
        lines.append(
            f"{uid} | {chain.get('chain_rank')} | {chain.get('chain_incentive')} | "
            f"{chain.get('stake')} | {chain.get('trust', 'N/A')} | {chain.get('hotkey')} | "
            f"{chain.get('coldkey')} | "
            f"{wandb_rank} | {avg_score} | {last_score} | {records} | {recent_scores}"
        )
    return "\n".join(lines)


class DiscordChatClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self._chain_refresh_task: asyncio.Task | None = None
        self._announcement_task: asyncio.Task | None = None
        self._seen_announcement_ids: set[str] = set()
        self._last_top_miner_uid: int | None = None
        self._top_miner_startup_alert_sent = False

    async def setup_hook(self) -> None:
        self._chain_refresh_task = asyncio.create_task(self._scheduled_chain_refresh())
        self._announcement_task = asyncio.create_task(self._scheduled_announcements())

    async def close(self) -> None:
        if self._chain_refresh_task:
            self._chain_refresh_task.cancel()
        if self._announcement_task:
            self._announcement_task.cancel()
        await super().close()

    async def on_ready(self) -> None:
        logger.info(f"Discord bot online: {self.user} (id={self.user.id})")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self.user:
            return
        if not self._channel_allowed(message):
            return

        should_answer, is_reply = await self._should_answer(message)
        if not should_answer:
            return

        query = _strip_bot_mentions(message.content, self.user)
        if not query:
            prompt = "Reply with a question, or mention me with one."
            await message.reply(prompt, mention_author=False, suppress_embeds=True)
            return

        logger.info(
            "Discord chat request from %s in %s%s",
            message.author,
            getattr(message.channel, "id", "dm"),
            " via reply" if is_reply else "",
        )

        async with message.channel.typing():
            answer = await self._answer(query)

        await self._reply(message, answer)

    def _channel_allowed(self, message: discord.Message) -> bool:
        if isinstance(message.channel, discord.DMChannel):
            return True
        return not ALLOWED_CHANNEL_IDS or message.channel.id in ALLOWED_CHANNEL_IDS

    async def _should_answer(self, message: discord.Message) -> tuple[bool, bool]:
        if isinstance(message.channel, discord.DMChannel):
            return True, False

        is_mention = self.user in message.mentions if self.user else False
        is_reply = await self._is_reply_to_bot(message)
        return is_mention or is_reply, is_reply

    async def _is_reply_to_bot(self, message: discord.Message) -> bool:
        if not message.reference or not message.reference.message_id:
            return False

        resolved = message.reference.resolved
        if isinstance(resolved, discord.Message):
            return bool(self.user and resolved.author.id == self.user.id)

        try:
            replied = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
        return bool(self.user and replied.author.id == self.user.id)

    async def _answer(self, query: str) -> str:
        chain_parts = []

        uid = _extract_uid(query)
        if uid is not None:
            chain = get_uid_info(uid)
            wb = wandb_cache.get_uid_info(uid)
            chain_parts.append(_format_combined_uid(uid, chain, wb))
        elif _needs_all_uid_data(query):
            chain_parts.append(build_live_data_block())
            chain_parts.append(_format_combined_all_uids())
        elif _needs_live(query):
            chain_parts.append(build_live_data_block())
            chain_parts.append(_format_combined_top_miners())

        docs_context = all_docs_context() if _needs_docs(query) else ""
        return await ask_codex(query, "\n\n".join(chain_parts), "", docs_context)

    async def _reply(self, message: discord.Message, answer: str) -> None:
        chunks = _split_discord_message(answer or "I don't have that information right now.")
        await message.reply(chunks[0], mention_author=False, suppress_embeds=True)
        for chunk in chunks[1:]:
            await message.channel.send(chunk, suppress_embeds=True)

    async def _scheduled_announcements(self) -> None:
        await self.wait_until_ready()
        channel_ids = ANNOUNCEMENT_CHANNEL_IDS or ALLOWED_CHANNEL_IDS

        existing = await asyncio.to_thread(get_announcements)
        self._seen_announcement_ids = {
            a["id"] for a in existing if a.get("active", True) and a.get("id")
        }

        if not channel_ids:
            logger.warning(
                "Announcement auto-post disabled: set ANNOUNCEMENT_CHANNEL_IDS or ALLOWED_CHANNEL_IDS"
            )
            return

        logger.info("Announcement auto-post enabled for channels: %s", channel_ids)
        while not self.is_closed():
            try:
                announcements = await asyncio.to_thread(get_announcements)
                new_active = [
                    a for a in announcements
                    if a.get("active", True) and a.get("id") not in self._seen_announcement_ids
                ]
                new_active.sort(key=lambda a: a.get("added", ""))
                for announcement in new_active:
                    await self._post_announcement(announcement, channel_ids)
                    self._seen_announcement_ids.add(announcement["id"])
            except Exception as e:
                logger.error("Announcement auto-post error: %s", e)
            await asyncio.sleep(ANNOUNCEMENT_POLL_INTERVAL)

    async def _post_announcement(self, announcement: dict, channel_ids: list[int]) -> None:
        title = announcement.get("title", "Announcement")
        body = announcement.get("body", "")
        text = f"@everyone\n**{title}**\n\n{body}".strip()
        allowed_mentions = discord.AllowedMentions(everyone=True)
        for chunk in _split_discord_message(text):
            for channel_id in channel_ids:
                try:
                    channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                    if hasattr(channel, "send"):
                        await channel.send(
                            chunk,
                            suppress_embeds=True,
                            allowed_mentions=allowed_mentions,
                        )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.error("Failed to post announcement to channel %s: %s", channel_id, e)

    async def _post_top_miner_alert(
        self,
        previous_uid: int | None,
        top_miner: dict,
        channel_ids: list[int],
    ) -> None:
        uid = top_miner.get("uid")
        prefix = "@everyone\n" if TOP_MINER_ALERT_MENTION_EVERYONE else ""
        text = (
            f"{prefix}**Top miner changed**\n"
            f"Previous top UID: {previous_uid if previous_uid is not None else 'N/A'}\n"
            f"New top UID: {uid}\n"
            f"On-chain incentive: {top_miner.get('chain_incentive')}\n"
            f"Stake: {top_miner.get('stake')}\n"
            f"Hotkey: {top_miner.get('hotkey')}\n"
            f"Coldkey: {top_miner.get('coldkey')}"
        ).strip()
        allowed_mentions = discord.AllowedMentions(
            everyone=TOP_MINER_ALERT_MENTION_EVERYONE
        )
        for chunk in _split_discord_message(text):
            for channel_id in channel_ids:
                try:
                    channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                    if hasattr(channel, "send"):
                        await channel.send(
                            chunk,
                            suppress_embeds=True,
                            allowed_mentions=allowed_mentions,
                        )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.error("Failed to post top miner alert to channel %s: %s", channel_id, e)

    async def _post_top_miner_startup_alert(
        self,
        top_miner: dict,
        channel_ids: list[int],
    ) -> None:
        text = (
            "**Current top miner**\n"
            "Top miner monitoring started.\n"
            f"UID: {top_miner.get('uid')}\n"
            f"On-chain incentive: {top_miner.get('chain_incentive')}"
        ).strip()
        allowed_mentions = discord.AllowedMentions.none()
        for chunk in _split_discord_message(text):
            for channel_id in channel_ids:
                try:
                    channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                    if hasattr(channel, "send"):
                        await channel.send(
                            chunk,
                            suppress_embeds=True,
                            allowed_mentions=allowed_mentions,
                        )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.error("Failed to post top miner startup alert to channel %s: %s", channel_id, e)

    async def _scheduled_chain_refresh(self) -> None:
        await self.wait_until_ready()
        alert_channel_ids = (
            TOP_MINER_ALERT_CHANNEL_IDS
            or ANNOUNCEMENT_CHANNEL_IDS
            or ALLOWED_CHANNEL_IDS
        )
        if TOP_MINER_ALERTS_ENABLED and not alert_channel_ids:
            logger.warning(
                "Top miner alerts disabled: set TOP_MINER_ALERT_CHANNEL_IDS, ANNOUNCEMENT_CHANNEL_IDS, or ALLOWED_CHANNEL_IDS"
            )
        while not self.is_closed():
            logger.info("Refreshing Bittensor chain cache...")
            try:
                data = await asyncio.to_thread(refresh_subnet_info)
                top_miner = data.get("top_miner") if isinstance(data, dict) else None
                if TOP_MINER_ALERTS_ENABLED and alert_channel_ids and top_miner:
                    top_uid = top_miner.get("uid")
                    if self._last_top_miner_uid is None:
                        self._last_top_miner_uid = top_uid
                        logger.info("Initial top miner observed: UID %s", top_uid)
                        if (
                            TOP_MINER_STARTUP_ALERT_ENABLED
                            and TOP_MINER_ALERT_CHANNEL_IDS
                            and not self._top_miner_startup_alert_sent
                        ):
                            await self._post_top_miner_startup_alert(
                                top_miner,
                                TOP_MINER_ALERT_CHANNEL_IDS,
                            )
                            self._top_miner_startup_alert_sent = True
                    elif top_uid != self._last_top_miner_uid:
                        previous_uid = self._last_top_miner_uid
                        self._last_top_miner_uid = top_uid
                        logger.info(
                            "Top miner changed: UID %s -> UID %s",
                            previous_uid,
                            top_uid,
                        )
                        await self._post_top_miner_alert(
                            previous_uid,
                            top_miner,
                            alert_channel_ids,
                        )
            except Exception as e:
                logger.error(f"Chain cache refresh error: {e}")
            await asyncio.sleep(CHAIN_POLL_INTERVAL)
