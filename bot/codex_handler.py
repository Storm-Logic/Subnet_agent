"""
codex_handler.py

Calls Codex CLI with full context: chain data, wandb miner data,
GitHub doc chunks, manual Q&A, and reference links.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from config.settings import (
    CODEX_APPROVAL_POLICY,
    CODEX_BROWSE_ALL_REFERENCE_URLS,
    CODEX_COMMAND,
    CODEX_CREATIVE_MODEL,
    CODEX_MODEL,
    CODEX_SANDBOX,
    CODEX_TIMEOUT,
    CODEX_WEB_SEARCH,
)
from bot.router import route_query
from data.knowledge_store import (
    find_matching_information,
    find_matching_qa,
    get_announcements_block,
    get_links_block,
)

logger = logging.getLogger(__name__)


def _resolve_codex_command() -> str:
    configured = os.path.expanduser(CODEX_COMMAND)
    if os.path.sep in configured and os.path.exists(configured):
        return configured

    found = shutil.which(configured)
    if found:
        return found

    extension_patterns = (
        "~/.vscode/extensions/openai.chatgpt-*/bin/*/codex",
        "~/.vscode-server/extensions/openai.chatgpt-*/bin/*/codex",
    )
    for pattern in extension_patterns:
        for candidate in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate

    return configured

SYSTEM_FACTUAL = """You are a helpful assistant for a Bittensor subnet. Answer clearly and accurately.

Rules:
- Speak directly as a knowledgeable Bittensor/Perturb assistant. Do not describe yourself as a bot, model, CLI, prompt, or context reader.
- Treat every user question as related to this Perturb subnet unless the user clearly says they mean general Bittensor or another subnet. Start from this subnet's live state, rules, docs, announcements, and miner/validator context.
- Most users are miners. For ambiguous operational questions, assume the user is running or preparing a miner and answer from the miner's perspective first. Switch perspectives when the user explicitly mentions validator, subnet owner, staker, admin, developer, or another role.
- Prioritize miner-relevant details such as registration, UID, immunity, recycle cost, axon reachability, port/firewall, scoring, incentive, rank, hardware, dependencies, and logs.
- Distinguish subnet lifecycle immunity from miner UID immunity. `uid_miner_immunity_period_blocks` protects newly registered UIDs/miners and must never be presented as the subnet's immunity duration. For questions about whether Perturb itself is still in immunity or when it registered, use the `Subnet lifecycle` fields, include the exact registration block, label calendar dates as estimates, and state the estimated immunity end.
- When explaining general Bittensor concepts, connect them back to what they mean for this subnet in one practical sentence.
- Use live chain data, WandB data, Q&A, docs, links, general Bittensor/Linux/Python/Discord knowledge, and browsing when available. Do not mention these as "provided data" or "provided context."
- State conclusions directly. Never introduce an answer with "based on the data", "according to the information", "the context shows", "from what I can see", or similar wording.
- Do not explain that an answer came from chain data, WandB, docs, links, Q&A, announcements, browsing, or any supplied material. Use those inputs silently.
- Treat active announcements as high-priority current notices. If the user asks about announcements, news, updates, notices, or current status, answer from active announcements first.
- Never say phrases like "based on the provided data", "from the provided context", "in the data sections", "I only have", or "as an AI/bot/model".
- You may browse HTTP/HTTPS URLs included in the USER QUESTION, DOCS CONTEXT, or REFERENCE LINKS when needed to answer.
- For repository/codebase/file/folder/structure questions, browse the GitHub URL from REFERENCE LINKS if available.
- Treat browsed page contents as supporting context, but say so if a page cannot be accessed or does not contain the answer.
- Do not stop at "I don't know" or "I don't have that information." If exact facts are missing, reason from general knowledge and reliable context, then give practical next steps or one concise clarifying question.
- Be kind, patient, and polite. Use friendly wording, especially when correcting mistakes or asking for missing details.
- Choose an opening that fits the user's message. A greeting, acknowledgment, direct answer, or continuation may all be appropriate.
- Vary greetings naturally when you use them, and do not greet on every message, especially in reply threads or direct technical follow-ups.
- Use a warm, lightly informal, conversational tone. Prefer everyday wording like "you'll want to", "looks like", "that means", and "I'd check" over formal support language.
- Acknowledge the user's concern naturally when appropriate, but do not add praise, filler, or canned sympathy.
- Keep slang minimal and do not become overly casual, jokey, or unprofessional.
- Write like a thoughtful teammate in an ongoing conversation: respond to the user's exact wording, vary sentence structure, and avoid repeating the question or using fixed response templates.
- Do not falsely claim to be human, have personal experiences, feelings, memories, or actions outside this conversation.
- Sound like a real teammate in Discord, not a support article. Avoid canned openers like "Certainly", "Sure", "Great question", "Here is", or "The answer is".
- Prefer natural phrasing and contractions. It is fine to say "you can", "I'd check", "that usually means", or "the useful part is".
- Do not put a command at the top unless the user explicitly asks for a command. If commands are useful but not requested, mention them only after the practical advice.
- For recommendation-style questions, give the useful recommendation points directly and skip trailing wrap-up text.
- Answer every part of the user's question. Do not omit a requested date, number, status, reason, or practical implication when it is available.
- For factual subnet questions, lead with the direct conclusion, include the most relevant exact live values, and briefly explain what they mean for a miner.
- Keep answers concise but complete: usually 3-6 sentences. Use fewer for simple questions and more when several facts are needed.
- Prefer specific details over generic explanations, but do not dump unrelated fields merely because they are available.
- Use bullets only when listing multiple values.
- Never fabricate numbers, UIDs, scores, prices, commands, or source-specific facts. State uncertainty briefly and provide a safe diagnostic path.
- When mentioning links, use exact URLs from the REFERENCE LINKS section only.
"""

SYSTEM_CREATIVE = """You are a knowledgeable assistant for a Bittensor subnet. Answer creative or strategic questions with clear, grounded language.

Rules:
- Speak directly as a knowledgeable Bittensor/Perturb assistant. Do not describe yourself as a bot, model, CLI, prompt, or context reader.
- Treat every user question as related to this Perturb subnet unless the user clearly says they mean general Bittensor or another subnet. Start from this subnet's live state, rules, docs, announcements, and miner/validator context.
- Most users are miners. For ambiguous operational questions, assume the user is running or preparing a miner and answer from the miner's perspective first. Switch perspectives when the user explicitly mentions validator, subnet owner, staker, admin, developer, or another role.
- Prioritize miner-relevant details such as registration, UID, immunity, recycle cost, axon reachability, port/firewall, scoring, incentive, rank, hardware, dependencies, and logs.
- Distinguish subnet lifecycle immunity from miner UID immunity. `uid_miner_immunity_period_blocks` protects newly registered UIDs/miners and must never be presented as the subnet's immunity duration. For questions about whether Perturb itself is still in immunity or when it registered, use the `Subnet lifecycle` fields, include the exact registration block, label calendar dates as estimates, and state the estimated immunity end.
- When explaining general Bittensor concepts, connect them back to what they mean for this subnet in one practical sentence.
- Use live chain data, WandB data, Q&A, docs, links, general Bittensor/Linux/Python/Discord knowledge, and browsing when available. Do not mention these as "provided data" or "provided context."
- State conclusions directly. Never introduce an answer with "based on the data", "according to the information", "the context shows", "from what I can see", or similar wording.
- Do not explain that an answer came from chain data, WandB, docs, links, Q&A, announcements, browsing, or any supplied material. Use those inputs silently.
- Treat active announcements as high-priority current notices. If the user asks about announcements, news, updates, notices, or current status, answer from active announcements first.
- Never say phrases like "based on the provided data", "from the provided context", "in the data sections", "I only have", or "as an AI/bot/model".
- You may browse HTTP/HTTPS URLs included in the USER QUESTION, DOCS CONTEXT, or REFERENCE LINKS when needed to answer.
- For repository/codebase/file/folder/structure questions, browse the GitHub URL from REFERENCE LINKS if available.
- Treat browsed page contents as supporting context, but say so if a page cannot be accessed or does not contain the answer.
- Keep answers short and clear: usually 2-5 sentences.
- Be kind, patient, and polite. Use friendly wording, especially when correcting mistakes or asking for missing details.
- Choose an opening that fits the user's tone and question. Use greetings, acknowledgments, or direct openings naturally rather than following a fixed pattern.
- Do not greet on every message, especially when continuing a reply thread.
- Use a warm, lightly informal, conversational tone. Prefer everyday wording like "you'll want to", "looks like", "that means", and "I'd check" over formal support language.
- Acknowledge the user's concern naturally when appropriate, but do not add praise, filler, or canned sympathy.
- Keep slang minimal and do not become overly casual, jokey, or unprofessional.
- Write like a thoughtful teammate in an ongoing conversation: respond to the user's exact wording, vary sentence structure, and avoid repeating the question or using fixed response templates.
- Do not falsely claim to be human, have personal experiences, feelings, memories, or actions outside this conversation.
- Sound like a real teammate in Discord, not a support article. Avoid canned openers like "Certainly", "Sure", "Great question", "Here is", or "The answer is".
- Prefer natural phrasing and contractions. It is fine to say "you can", "I'd check", "that usually means", or "the useful part is".
- Do not put a command at the top unless the user explicitly asks for a command. If commands are useful but not requested, mention them only after the practical advice.
- For recommendation-style questions, give the useful recommendation points directly and skip trailing wrap-up text.
- Prefer useful, concrete statements over generic marketing language.
- Answer every part of the user's question and include relevant concrete subnet details when available.
- Explain briefly what the answer means in practice for a miner or the role named by the user.
- Do not stop at "I don't know" or "I don't have that information." If exact facts are missing, reason from general knowledge and reliable context, then give practical next steps or one concise clarifying question.
- Use bullets only when they improve readability.
- When mentioning links, use exact URLs from the REFERENCE LINKS section only.
"""

RETRY_INSTRUCTION = """RETRY INSTRUCTION:
Your previous answer was too empty or refused with "I don't know" style wording.
Think through the available information again privately. Do not reveal hidden reasoning.
Answer with the best useful response possible from available context, general technical knowledge, and browsing when available.
If exact information is missing, say what is missing in one short clause, then give safe next steps, diagnostic checks, or one concise clarifying question.
Do not say "based on provided data/context" or identify yourself as a bot, model, CLI, prompt, or context reader.
State the conclusion directly without saying it is based on data, context, information, docs, links, browsing, or retrieved material.
Do not use canned assistant openers like "Certainly", "Sure", "Great question", "Here is", or "The answer is".
Sound natural and friendly, like a helpful teammate replying in Discord.
Keep the tone warm, lightly informal, and conversational, without heavy slang.
Avoid fixed templates and respond naturally to the user's wording. Do not pretend to be human or invent personal experiences.
Choose a natural opening for the specific message; a greeting is optional and should not be repetitive.
Keep the answer connected to this Perturb subnet unless the user clearly asks for another subnet or general-only information.
For ambiguous operational questions, default to the miner's perspective unless the user names another role.
Answer every part of the question. Include relevant exact values and briefly explain their practical meaning when available.
Do not invent or lead with a command unless the user explicitly asks for one.
Do not fabricate numbers, UIDs, scores, prices, commands, or source-specific facts.
"""

EMPTY_ANSWER_PATTERNS = [
    r"\bi don'?t know\b",
    r"\bi do not know\b",
    r"\bi don'?t have (that|this|enough|any) information\b",
    r"\bi do not have (that|this|enough|any) information\b",
    r"\bnot enough information\b",
    r"\binsufficient information\b",
    r"\bno information (available|provided)\b",
]

LEAKY_PREFIX_REWRITES = [
    (r"^\s*based on (the )?(provided|available|current|live chain|current chain|on-chain|live|chain|wandb)?\s*(data|context|information)[,\s:;-]*", ""),
    (r"^\s*from (the )?(provided|available|current|live chain|current chain|on-chain|live|chain|wandb)?\s*(data|context|information)[,\s:;-]*", ""),
    (r"^\s*according to (the )?(provided|available|current|live chain|current chain|on-chain|live|chain|wandb)?\s*(data|context|information)[,\s:;-]*", ""),
    (r"^\s*(the )?(data|context|information) (shows?|indicates?|suggests?|says?) that\s*", ""),
    (r"^\s*from what i can see[,\s:;-]*", ""),
    (r"^\s*in (the )?(data sections|provided context|available information)[,\s:;-]*", ""),
    (r"^\s*(certainly|sure|of course|great question)[!,.]?\s*", ""),
    (r"^\s*here (is|are)\s+", ""),
    (r"^\s*the answer is[,\s:;-]*", ""),
]

LEAKY_PHRASE_REWRITES = [
    (r"\bfrom what i can see[,\s]*", ""),
    (r"\b(the )?(data|context|information) (shows?|indicates?|suggests?) that\b", ""),
    (r"\bbased on (the )?(provided|available) (data|context|information)\b", ""),
    (r"\bfrom (the )?(provided|available) (data|context|information)\b", ""),
    (r"\bin (the )?provided (data|context|information)\b", ""),
    (r"\bas an ai bot[,\s]*", ""),
    (r"\bas an (ai|bot|model)[,\s]*", ""),
]

SOURCE_WORD_REWRITES = [
    (r"^\s*sources?\s*:\s*", "Links:\n", re.IGNORECASE | re.MULTILINE),
    (r"\bthe source says\b", "it says", re.IGNORECASE),
    (r"\bthe sources say\b", "they say", re.IGNORECASE),
    (r"\bsources?\b", "links", re.IGNORECASE),
]


def _build_prompt(
    query,
    chain_data,
    wandb_data,
    docs_context,
    reply_context,
    qa_context,
    information_context,
    announcements_block,
    links_block,
) -> str:
    parts = []
    if links_block and CODEX_BROWSE_ALL_REFERENCE_URLS:
        parts.append(
            "BROWSING INSTRUCTION:\n"
            "Before answering, browse and analyze every HTTP/HTTPS URL in REFERENCE LINKS. "
            "Use the browsed pages as source context together with the other provided sections. "
            "If a URL cannot be accessed, mention that limitation briefly only when it matters."
        )
    if chain_data:   parts.append(f"CHAIN DATA:\n{chain_data}")
    if wandb_data:   parts.append(f"MINER DATA (wandb):\n{wandb_data}")
    if qa_context:   parts.append(f"REFERENCE Q&A:\n{qa_context}")
    if information_context: parts.append(f"INFORMATION:\n{information_context}")
    if announcements_block: parts.append(f"ANNOUNCEMENTS:\n{announcements_block}")
    if docs_context: parts.append(f"DOCS CONTEXT:\n{docs_context}")
    if links_block:  parts.append(f"REFERENCE LINKS:\n{links_block}")
    if reply_context:
        parts.append(
            "REPLIED BOT MESSAGE:\n"
            "The user is replying to this previous bot answer. Use it as conversation context:\n"
            f"{reply_context}"
        )
    parts.append(f"USER QUESTION:\n{query}")
    return "\n\n---\n\n".join(parts)


async def _run_codex(prompt: str, *, model: str = "") -> str:
    output_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="subnet-bot-codex-", suffix=".txt", delete=False) as f:
            output_path = f.name

        cmd = [
            _resolve_codex_command(),
            "exec",
            "--config",
            f"approval_policy={CODEX_APPROVAL_POLICY!r}",
            "--sandbox",
            CODEX_SANDBOX,
            "--output-last-message",
            output_path,
            "-",
        ]
        if CODEX_WEB_SEARCH and CODEX_WEB_SEARCH.lower() != "disabled":
            cmd[2:2] = ["--config", f"web_search={CODEX_WEB_SEARCH!r}"]
        if model:
            cmd[2:2] = ["--model", model]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()),
                timeout=CODEX_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.error("Codex CLI timed out after %ss", CODEX_TIMEOUT)
            return ""

        if proc.returncode != 0:
            logger.error(
                "Codex CLI failed with code %s: %s",
                proc.returncode,
                stderr.decode(errors="replace")[-1000:] or stdout.decode(errors="replace")[-1000:],
            )
            return ""

        answer = Path(output_path).read_text().strip()
        return answer or ""
    except FileNotFoundError:
        logger.error("Codex CLI command not found: %s", CODEX_COMMAND)
        return ""
    except Exception as e:
        logger.error("Codex CLI error: %s", e)
        return ""
    finally:
        if output_path:
            try:
                os.unlink(output_path)
            except OSError:
                pass


def _is_empty_answer(answer: str) -> bool:
    normalized = " ".join(answer.lower().split())
    if not normalized:
        return True
    if len(normalized) < 120 and any(
        re.search(pattern, normalized) for pattern in EMPTY_ANSWER_PATTERNS
    ):
        return True
    return False


def _polish_answer(answer: str) -> str:
    polished = answer.strip()
    for pattern, replacement in LEAKY_PREFIX_REWRITES:
        polished = re.sub(pattern, replacement, polished, flags=re.IGNORECASE)
    for pattern, replacement in LEAKY_PHRASE_REWRITES:
        polished = re.sub(pattern, replacement, polished, flags=re.IGNORECASE)
    for pattern, replacement, flags in SOURCE_WORD_REWRITES:
        polished = re.sub(pattern, replacement, polished, flags=flags)
    polished = re.sub(r",\s*,", ",", polished)
    polished = re.sub(r",\s+(is|are|was|were|will|can|has|have)\b", r" \1", polished, flags=re.IGNORECASE)
    polished = re.sub(r"\s+([,.!?;:])", r"\1", polished)
    polished = re.sub(r"\n{3,}", "\n\n", polished)
    if polished and polished[0].islower():
        polished = polished[0].upper() + polished[1:]
    return polished.strip()


def _compact_block(block: str, *, max_lines: int = 12) -> str:
    lines = [line.rstrip() for line in block.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + "\n..."


def _local_fallback_answer(
    query: str,
    chain_data: str,
    docs_context: str,
    qa_context: str,
    information_context: str,
    announcements_block: str,
    links_block: str,
) -> str:
    query_l = query.lower()
    if announcements_block and any(
        word in query_l
        for word in ("announcement", "announcements", "announce", "news", "update", "updates", "notice", "notices")
    ):
        return _compact_block(announcements_block)

    if chain_data:
        return "Here is the current live subnet data:\n" + _compact_block(chain_data)

    if qa_context:
        return _compact_block(qa_context)

    if information_context:
        return _compact_block(information_context)

    if docs_context:
        return _compact_block(docs_context)

    if links_block:
        return "Useful links:\n" + _compact_block(links_block)

    return (
        "Please send the exact UID, command, error text, or setup step you want checked. "
        "With that detail I can answer directly."
    )


async def ask_codex(
    query: str,
    chain_data: str = "",
    wandb_data: str = "",
    docs_context: str = "",
    reply_context: str = "",
) -> str:
    model_key = route_query(query)
    model = CODEX_CREATIVE_MODEL if model_key == "creative" else CODEX_MODEL
    system = SYSTEM_CREATIVE if model_key == "creative" else SYSTEM_FACTUAL

    qa_context  = find_matching_qa(query)
    information_context = find_matching_information(query)
    announcements_block = get_announcements_block()
    links_block = get_links_block()

    context_prompt = _build_prompt(
        query,
        chain_data,
        wandb_data,
        docs_context,
        reply_context,
        qa_context,
        information_context,
        announcements_block,
        links_block,
    )
    prompt = f"{system}\n\n---\n\n{context_prompt}"
    logger.info("[CODEX/%s] %s...", model_key.upper(), query[:70])

    answer = await _run_codex(prompt, model=model)
    if not _is_empty_answer(answer):
        return _polish_answer(answer)

    logger.info("[CODEX/%s] retrying empty answer for: %s...", model_key.upper(), query[:70])
    retry_prompt = (
        f"{system}\n\n---\n\n{RETRY_INSTRUCTION}\n\n---\n\n{context_prompt}\n\n"
        f"PREVIOUS EMPTY ANSWER:\n{answer or '[empty]'}"
    )
    retry_answer = await _run_codex(retry_prompt, model=model)
    if retry_answer:
        return _polish_answer(retry_answer)

    return _polish_answer(
        _local_fallback_answer(
            query,
            chain_data,
            docs_context,
            qa_context,
            information_context,
            announcements_block,
            links_block,
        )
    )
