import os
from dotenv import load_dotenv

load_dotenv()

# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")

def _parse_channel_ids(value: str) -> list[int]:
    ids = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    return ids

ALLOWED_CHANNEL_IDS = _parse_channel_ids(os.getenv("ALLOWED_CHANNEL_IDS", ""))
ANNOUNCEMENT_CHANNEL_IDS = _parse_channel_ids(os.getenv("ANNOUNCEMENT_CHANNEL_IDS", ""))
ANNOUNCEMENT_POLL_INTERVAL = int(os.getenv("ANNOUNCEMENT_POLL_INTERVAL", "15"))
TOP_MINER_ALERT_CHANNEL_IDS = _parse_channel_ids(os.getenv("TOP_MINER_ALERT_CHANNEL_IDS", ""))
TOP_MINER_ALERTS_ENABLED = os.getenv("TOP_MINER_ALERTS_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}
TOP_MINER_ALERT_MENTION_EVERYONE = os.getenv("TOP_MINER_ALERT_MENTION_EVERYONE", "true").lower() in {
    "1", "true", "yes", "on"
}
TOP_MINER_STARTUP_ALERT_ENABLED = os.getenv("TOP_MINER_STARTUP_ALERT_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}

# ── Codex CLI ─────────────────────────────────────────────────────────────────
CODEX_COMMAND         = os.getenv("CODEX_COMMAND", "codex")
CODEX_MODEL           = os.getenv("CODEX_MODEL", "")
CODEX_CREATIVE_MODEL  = os.getenv("CODEX_CREATIVE_MODEL", CODEX_MODEL)
CODEX_TIMEOUT         = int(os.getenv("CODEX_TIMEOUT", "120"))
CODEX_SANDBOX         = os.getenv("CODEX_SANDBOX", "read-only")
CODEX_APPROVAL_POLICY = os.getenv("CODEX_APPROVAL_POLICY", "never")
CODEX_WEB_SEARCH      = os.getenv("CODEX_WEB_SEARCH", "live")
CODEX_BROWSE_ALL_REFERENCE_URLS = os.getenv("CODEX_BROWSE_ALL_REFERENCE_URLS", "true").lower() in {
    "1", "true", "yes", "on"
}

# ── Bittensor ─────────────────────────────────────────────────────────────────
NETUID             = int(os.getenv("NETUID", "1"))
SUBTENSOR_NETWORK  = os.getenv("SUBTENSOR_NETWORK", "finney")
CHAIN_POLL_INTERVAL = int(os.getenv("CHAIN_POLL_INTERVAL", "300"))
CACHE_TTL_PRICE    = int(os.getenv("CACHE_TTL_PRICE", str(CHAIN_POLL_INTERVAL * 2)))
CACHE_TTL_MINERS   = 300

# ── wandb (no run IDs — auto-discovered) ─────────────────────────────────────
WANDB_API_KEY       = os.getenv("WANDB_API_KEY")
WANDB_ENTITY        = os.getenv("WANDB_ENTITY", "")
WANDB_PROJECT       = os.getenv("WANDB_PROJECT", "")
WANDB_POLL_INTERVAL = int(os.getenv("WANDB_POLL_INTERVAL", "120"))   # seconds
WANDB_TARGET_RECORDS = int(os.getenv("WANDB_TARGET_RECORDS", "100")) # stop fetching runs after this many records
WANDB_MAX_RUNS       = int(os.getenv("WANDB_MAX_RUNS", "10"))        # hard cap per cycle
WANDB_API_TIMEOUT    = int(os.getenv("WANDB_API_TIMEOUT", "60"))     # wandb graphql timeout seconds

# ── GitHub docs ───────────────────────────────────────────────────────────────
GITHUB_REPO   = os.getenv("GITHUB_REPO", "")
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
DOCS_PATHS    = [p.strip() for p in os.getenv("DOCS_PATHS", "README.md").split(",") if p.strip()]

# ── Admin UI ──────────────────────────────────────────────────────────────────
ADMIN_UI_TOKEN = os.getenv("ADMIN_UI_TOKEN", "changeme")
ADMIN_PORT     = int(os.getenv("ADMIN_PORT", "8888"))
