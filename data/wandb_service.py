"""
wandb_service.py

Background poller that calls wandb_extractor on each cycle
and maintains a thread-safe cache for the bot to query instantly.
"""

from __future__ import annotations

import logging
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional

from data.wandb_extractor import discover_and_fetch, ROLLING_WINDOW, TOP_N

logger = logging.getLogger(__name__)


class WandBCache:
    """Thread-safe in-memory cache of the latest miner rankings."""

    def __init__(self):
        self._lock = threading.RLock()
        self._rankings: List[dict] = []          # sorted by rank
        self._uid_index: Dict[int, dict] = {}    # uid -> row for O(1) lookup
        self._active_run_ids: List[str] = []
        self._last_updated: Optional[float] = None

    def update(self, rankings: List[dict], run_ids: List[str]):
        with self._lock:
            self._rankings = rankings
            self._uid_index = {r["uid"]: r for r in rankings}
            self._active_run_ids = run_ids
            self._last_updated = time.time()
            logger.info(
                "WandB cache updated: miners=%s runs=%s top_uid=%s top_avg=%s",
                len(rankings),
                len(run_ids),
                rankings[0]["uid"] if rankings else "N/A",
                f"{rankings[0]['avg_score']:.6f}" if rankings else "N/A",
            )

    # ── Bot-facing query methods ──────────────────────────────────────────────

    def get_top_miners(self, n: int = 10) -> List[dict]:
        with self._lock:
            return list(self._rankings[:n])

    def get_uid_info(self, uid: int) -> Optional[dict]:
        with self._lock:
            return self._uid_index.get(uid)

    def get_summary_block(self) -> str:
        """Compact text block injected into LLM prompts."""
        with self._lock:
            if not self._rankings:
                return "[WandB miner data: not yet available]"
            updated = (
                datetime.utcfromtimestamp(self._last_updated).strftime("%Y-%m-%d %H:%M UTC")
                if self._last_updated else "unknown"
            )
            lines = [
                f"[WandB miner data — updated {updated}]",
                f"Runs used this cycle: {', '.join(self._active_run_ids)}",
                f"Top {min(TOP_N, len(self._rankings))} miners "
                f"by avg score (rolling window: last {ROLLING_WINDOW} records):",
            ]
            for row in self._rankings[:TOP_N]:
                lines.append(
                    f"  #{row['rank']} UID {row['uid']} — "
                    f"avg {row['avg_score']:.6f}, last {row['last_score']:.6f}, "
                    f"n={row['records']}"
                )
            return "\n".join(lines)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def active_run_ids(self) -> List[str]:
        with self._lock:
            return list(self._active_run_ids)

    @property
    def last_updated(self) -> Optional[str]:
        with self._lock:
            if self._last_updated is None:
                return None
            return datetime.utcfromtimestamp(self._last_updated).strftime("%Y-%m-%d %H:%M UTC")

    @property
    def is_stale(self) -> bool:
        with self._lock:
            if self._last_updated is None:
                return True
            return (time.time() - self._last_updated) > 600   # 10 min


# Singleton shared across the whole app
wandb_cache = WandBCache()


class WandBPoller:
    """
    Runs discover_and_fetch on a background thread every `interval` seconds.
    No run IDs needed — they are discovered automatically from the project.
    """

    def __init__(
        self,
        entity: str,
        project: str,
        interval: int = 120,
        target_records: int = 100,
        max_runs: int = 10,
        api_timeout: int = 60,
    ):
        self.entity = entity
        self.project = project
        self.interval = interval
        self.target_records = target_records
        self.max_runs = max_runs
        self.api_timeout = api_timeout
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _cycle(self):
        logger.info("WandB poll cycle started")
        rankings, run_ids = discover_and_fetch(
            entity=self.entity,
            project=self.project,
            target_records=self.target_records,
            max_runs=self.max_runs,
            api_timeout=self.api_timeout,
        )
        if rankings:
            wandb_cache.update(rankings, run_ids)
        else:
            logger.warning("WandB poll cycle completed with no rankings")

    def _loop(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                self._cycle()
            except Exception as e:
                logger.error(f"WandB poll cycle error: {e}")
            elapsed = time.time() - started
            logger.info(
                "Next WandB poll in %ss (last cycle %.1fs)",
                self.interval,
                elapsed,
            )
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="wandb-poller"
        )
        self._thread.start()
        logger.info(
            f"WandB poller started — "
            f"{self.entity}/{self.project} | "
            f"interval={self.interval}s | "
            f"target_records={self.target_records} | "
            f"max_runs={self.max_runs} | "
            f"api_timeout={self.api_timeout}s"
        )

    def stop(self):
        self._stop.set()
