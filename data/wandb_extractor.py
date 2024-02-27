"""
wandb_extractor.py

Extracts (uid, score) data from a wandb project for the subnet Discord bot.
Derived from the reference monitoring script, stripped down to what the bot needs:
  - Auto-discover recent runs from the project (no manual run ID config)
  - Extract uid/score pairs from log lines and run history
  - Combine across runs until we have enough records (~100 per uid window)
  - Return structured rank/score data ready for the bot cache
"""

from __future__ import annotations

import re
import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Deque

import wandb

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

ROLLING_WINDOW = 50   # last N scores per uid to keep
TOP_N = 10            # top miners to surface

# ── Regex patterns (from reference script) ───────────────────────────────────

SCORE_LINE_RE = re.compile(
    r"\buid=(?P<uid>\d+)\b.*?\bscore=(?P<score>-?\d+(?:\.\d+)?)\b"
)
LINE_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")

# ── GQL query for paginated log lines ────────────────────────────────────────

RUN_LOG_LINES_QUERY = """
query RunLogLines(
  $entity: String!,
  $project: String!,
  $name: String!,
  $first: Int,
  $after: String
) {
  project(name: $project, entityName: $entity) {
    run(name: $name) {
      logLines(first: $first, after: $after, useImprovedPagination: true) {
        edges {
          cursor
          node { line }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""


# ── Type aliases ──────────────────────────────────────────────────────────────

# uid -> deque of (timestamp, score)
ScoreMap = Dict[int, Deque[Tuple[float, float]]]
# uid -> list of (timestamp, score), sorted
EntryMap = Dict[int, List[Tuple[float, float]]]


# ── Coercion helpers ──────────────────────────────────────────────────────────

def _to_int(v) -> Optional[int]:
    if v is None: return None
    if isinstance(v, int): return v
    if isinstance(v, float): return int(v)
    if isinstance(v, str):
        v = v.strip()
        try: return int(float(v))
        except ValueError: return None
    return None

def _to_float(v) -> Optional[float]:
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        v = v.strip()
        try: return float(v)
        except ValueError: return None
    return None


# ── Extraction from a single log line ────────────────────────────────────────

def _uid_score_from_line(line: str) -> Optional[Tuple[int, float]]:
    m = SCORE_LINE_RE.search(line)
    if not m:
        return None
    try:
        return int(m.group("uid")), float(m.group("score"))
    except ValueError:
        return None

def _ts_from_line(line: str) -> Optional[float]:
    m = LINE_TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except ValueError:
        return None

def _uid_score_from_history_row(row: dict) -> Optional[Tuple[int, float]]:
    uid = _to_int(row.get("uid"))
    score = _to_float(row.get("score"))
    if uid is not None and score is not None:
        return uid, score
    msg = row.get("validator/console_message")
    if isinstance(msg, str):
        m = SCORE_LINE_RE.search(msg)
        if m:
            try:
                return int(m.group("uid")), float(m.group("score"))
            except ValueError:
                pass
    return None


# ── Fetch from a single run ───────────────────────────────────────────────────

def _fetch_from_log_lines(run: wandb.apis.public.Run, page_size: int = 500) -> ScoreMap:
    """Primary source: paginated log lines (matches wandb Logs view)."""
    scores: ScoreMap = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    after: Optional[str] = None

    while True:
        try:
            resp = run._service_api.execute_graphql(
                RUN_LOG_LINES_QUERY,
                variables={
                    "entity": run.entity,
                    "project": run.project,
                    "name": run.id,
                    "first": page_size,
                    "after": after,
                },
            )
        except Exception as e:
            logger.warning(f"Log lines query failed for {run.id}: {e}")
            break

        run_obj = (resp or {}).get("project", {}).get("run")
        if not run_obj:
            break

        log_lines = run_obj.get("logLines") or {}
        edges = log_lines.get("edges") or []
        if not edges:
            break

        for edge in edges:
            line = ((edge.get("node") or {}).get("line")) or ""
            if not line:
                continue
            pair = _uid_score_from_line(line)
            if pair is None:
                continue
            uid, score = pair
            ts = _ts_from_line(line) or float(len(scores[uid]))
            scores[uid].append((ts, score))

        page_info = log_lines.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        after = page_info["endCursor"]

    return scores


def _fetch_from_history(run: wandb.apis.public.Run, page_size: int = 2000) -> ScoreMap:
    """Fallback source: run history stream."""
    scores: ScoreMap = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    try:
        for row in run.scan_history(page_size=page_size):
            pair = _uid_score_from_history_row(row)
            if pair is None:
                continue
            uid, score = pair
            ts = _to_float(row.get("_timestamp")) or float(len(scores[uid]))
            scores[uid].append((ts, score))
    except Exception as e:
        logger.warning(f"History scan failed for {run.id}: {e}")
    return scores


def fetch_run_scores(run: wandb.apis.public.Run) -> ScoreMap:
    """Try log lines first, fall back to history."""
    scores = _fetch_from_log_lines(run)
    if scores:
        return scores
    return _fetch_from_history(run)


# ── Combine across runs ───────────────────────────────────────────────────────

def combine_runs(score_maps: List[ScoreMap]) -> EntryMap:
    """
    Merge uid entries across multiple runs.
    Keeps the latest ROLLING_WINDOW (timestamp, score) pairs per uid globally.
    """
    raw: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    for sm in score_maps:
        for uid, entries in sm.items():
            raw[uid].extend(list(entries))

    result: EntryMap = {}
    for uid, entries in raw.items():
        entries.sort(key=lambda x: x[0])
        result[uid] = entries[-ROLLING_WINDOW:]
    return result


# ── Ranking ───────────────────────────────────────────────────────────────────

def _avg(vals) -> float:
    lst = list(vals)
    return sum(lst) / len(lst) if lst else 0.0

def compute_rankings(entry_map: EntryMap) -> List[dict]:
    """
    Returns list of dicts sorted by avg_score desc.
    Each dict: uid, rank, avg_score, last_score, records, recent_scores
    """
    rows = []
    for uid, entries in entry_map.items():
        scores = [s for _, s in entries]
        rows.append({
            "uid": uid,
            "avg_score": _avg(scores),
            "last_score": scores[-1] if scores else 0.0,
            "records": len(scores),
            "recent_scores": scores[-ROLLING_WINDOW:],
        })

    rows.sort(key=lambda r: r["avg_score"], reverse=True)
    for i, row in enumerate(rows):
        row["rank"] = i + 1

    return rows


# ── Run discovery ─────────────────────────────────────────────────────────────

def discover_and_fetch(
    entity: str,
    project: str,
    target_records: int = 100,
    max_runs: int = 10,
    api_timeout: int = 60,
) -> Tuple[List[dict], List[str]]:
    """
    Top-level function the bot calls each poll cycle.

    1. Queries the project for recent running/finished runs (newest first)
    2. Fetches runs until cumulative records >= target_records
    3. Combines, ranks, returns (rankings, used_run_ids)

    Returns:
        rankings  — list of uid dicts sorted by avg_score desc
        run_ids   — which run IDs were actually used this cycle
    """
    api = wandb.Api(timeout=api_timeout)

    try:
        runs = api.runs(
            f"{entity}/{project}",
            filters={"state": {"$in": ["running", "finished"]}},
            order="-created_at",
            per_page=max_runs,
        )
    except Exception as e:
        logger.error(f"Failed to query runs from {entity}/{project}: {e}")
        return [], []

    score_maps: List[ScoreMap] = []
    used_run_ids: List[str] = []
    total_records = 0

    for run in runs:
        try:
            sm = fetch_run_scores(run)
            if not sm:
                logger.debug(f"Run {run.id} yielded no scores, skipping")
                continue

            score_maps.append(sm)
            used_run_ids.append(run.id)

            run_records = sum(len(v) for v in sm.values())
            total_records += run_records
            logger.info(f"Run {run.id}: {len(sm)} UIDs, {run_records} records (total so far: {total_records})")

            if total_records >= target_records:
                break

        except Exception as e:
            logger.error(f"Failed to fetch run {run.id}: {e}")
            continue

    if not score_maps:
        logger.warning(f"No usable runs found in {entity}/{project}")
        return [], []

    entry_map = combine_runs(score_maps)
    rankings = compute_rankings(entry_map)

    logger.info(
        f"wandb fetch complete: {len(rankings)} UIDs ranked, "
        f"{len(used_run_ids)} runs used {used_run_ids}"
    )
    return rankings, used_run_ids
