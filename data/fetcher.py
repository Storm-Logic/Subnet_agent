"""
fetcher.py

Fetches live data from the Bittensor chain.
All results are cached to avoid hammering the subtensor RPC on every query.
"""

from __future__ import annotations

import contextlib
import calendar
import importlib
import io
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config.settings import (
    NETUID, SUBTENSOR_NETWORK,
    CACHE_TTL_PRICE, CACHE_TTL_MINERS,
)

logger = logging.getLogger(__name__)

_cache: dict = {}
_bt = None
_recycle_lock = threading.RLock()
_recycle_history_file = Path(__file__).parent / "recycle_history.json"
BLOCKS_PER_DAY = 7200
AVERAGE_BLOCK_SECONDS = 12
SUBNET_IMMUNITY_MONTHS = 4

def _get(key: str, ttl: int):
    entry = _cache.get(key)
    if entry and (time.time() - entry[1]) < ttl:
        return entry[0]
    return None

def _set(key: str, value):
    _cache[key] = (value, time.time())


def _load_recycle_history() -> dict:
    if not _recycle_history_file.exists():
        return {"last_snapshot": None, "observations": []}
    try:
        return json.loads(_recycle_history_file.read_text())
    except Exception as e:
        logger.error("Recycle history load error: %s", e)
        return {"last_snapshot": None, "observations": []}


def _save_recycle_history(data: dict) -> None:
    _recycle_history_file.write_text(json.dumps(data, indent=2))


def _update_recycle_metrics(
    *,
    block: int,
    current_burn_tao: float,
    registrations_this_interval: int,
    burn_registrations_this_interval: int,
    target_regs_per_interval: int,
    adjustment_interval: int,
) -> dict:
    now = time.time()
    with _recycle_lock:
        history = _load_recycle_history()
        previous = history.get("last_snapshot") or {}
        previous_count = int(previous.get("burn_registrations_this_interval", 0))
        if burn_registrations_this_interval >= previous_count:
            new_registrations = burn_registrations_this_interval - previous_count
        else:
            new_registrations = burn_registrations_this_interval

        observations = [
            item for item in history.get("observations", [])
            if float(item.get("timestamp", 0)) >= now - 86400
        ]
        if new_registrations > 0:
            observations.append(
                {
                    "timestamp": now,
                    "block": block,
                    "registrations": new_registrations,
                    "recycle_tao_estimate": round(new_registrations * current_burn_tao, 9),
                }
            )

        history = {
            "last_snapshot": {
                "timestamp": now,
                "block": block,
                "burn_registrations_this_interval": burn_registrations_this_interval,
                "current_burn_tao": current_burn_tao,
            },
            "observations": observations,
        }
        _save_recycle_history(history)

    observed_registrations = sum(int(item.get("registrations", 0)) for item in observations)
    observed_recycle = sum(float(item.get("recycle_tao_estimate", 0)) for item in observations)
    projected_registrations = (
        target_regs_per_interval * BLOCKS_PER_DAY / adjustment_interval
        if adjustment_interval > 0 else 0
    )
    return {
        "current_recycle_cost_tao": round(current_burn_tao, 9),
        "registrations_this_interval": registrations_this_interval,
        "burn_registrations_this_interval": burn_registrations_this_interval,
        "observed_registrations_24h": observed_registrations,
        "observed_recycle_24h_tao_estimate": round(observed_recycle, 9),
        "projected_registrations_per_day": round(projected_registrations, 4),
        "projected_recycle_per_day_tao": round(projected_registrations * current_burn_tao, 9),
        "calculation_note": (
            "Observed 24h recycle is estimated from registration counter changes seen by this bot. "
            "Projected daily recycle uses current recycle cost and target registrations."
        ),
    }


def _bittensor():
    global _bt
    if _bt is not None:
        return _bt

    bt_logger = logging.getLogger("bittensor")
    was_disabled = bt_logger.disabled
    bt_logger.disabled = True
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            _bt = importlib.import_module("bittensor")
    finally:
        bt_logger.disabled = was_disabled
    return _bt


@contextlib.contextmanager
def _subtensor_client():
    """Open one Subtensor RPC connection and always close its WebSocket."""
    sub = _bittensor().Subtensor(network=SUBTENSOR_NETWORK)
    try:
        yield sub
    finally:
        try:
            sub.close()
        except Exception as e:
            logger.warning("Subtensor connection close error: %s", e)


def _subnet_float(subnet, *names: str) -> float:
    for name in names:
        value = getattr(subnet, name, None)
        if value is not None:
            return float(value)
    return 0.0


def _balance_tao(value) -> float | str:
    tao = getattr(value, "tao", None)
    if tao is not None:
        return round(float(tao), 9)
    try:
        return round(float(value), 9)
    except (TypeError, ValueError):
        return str(value)


def _balance_text(value) -> str:
    if value is None:
        return "N/A"
    return str(value)


def _rao_to_tao(value) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / 1_000_000_000, 9)
    except (TypeError, ValueError):
        return None


def _add_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _subnet_lifecycle(current_block: int, registered_at_block) -> dict:
    try:
        registration_block = int(registered_at_block)
    except (TypeError, ValueError):
        return {
            "network_registered_at_block": registered_at_block,
            "registration_time_utc_estimate": "N/A",
            "subnet_immunity_end_utc_estimate": "N/A",
            "subnet_immunity_status_estimate": "unknown",
        }

    now = datetime.now(timezone.utc)
    age_blocks = max(0, current_block - registration_block)
    registered_at = now - timedelta(seconds=age_blocks * AVERAGE_BLOCK_SECONDS)
    immunity_end = _add_calendar_months(registered_at, SUBNET_IMMUNITY_MONTHS)
    return {
        "network_registered_at_block": registration_block,
        "registration_time_utc_estimate": registered_at.strftime("%Y-%m-%d %H:%M UTC"),
        "subnet_age_blocks": age_blocks,
        "subnet_age_days_estimate": round(age_blocks / BLOCKS_PER_DAY, 2),
        "subnet_immunity_policy_months": SUBNET_IMMUNITY_MONTHS,
        "subnet_immunity_end_utc_estimate": immunity_end.strftime("%Y-%m-%d %H:%M UTC"),
        "subnet_immunity_status_estimate": (
            "in immunity" if now < immunity_end else "immunity ended"
        ),
        "calculation_note": (
            "Registration block is exact chain state. Dates are estimates using "
            f"{AVERAGE_BLOCK_SECONDS}-second average blocks. Subnet immunity is distinct "
            "from the per-UID miner immunity_period hyperparameter."
        ),
    }


def _metagraph_float(meta, uid: int, *names: str) -> float | None:
    for name in names:
        values = getattr(meta, name, None)
        if values is not None:
            return round(float(values[uid]), 4)
    return None


def _serialize_hyperparameters(hyperparameters) -> dict:
    raw = getattr(hyperparameters, "__dict__", {})
    result = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)
    return result


def _detailed_parameters(subnet, hyperparameters: dict, subnet_registration_cost=None) -> dict:
    """Readable aliases for commonly asked subnet/runtime fields."""
    return {
        "subnet_registration_cost": _balance_text(subnet_registration_cost),
        "subnet_registration_cost_tao": _balance_tao(subnet_registration_cost) if subnet_registration_cost is not None else "N/A",
        "uid_registration_allowed": hyperparameters.get("registration_allowed"),
        "uid_registration_min_burn_rao": hyperparameters.get("min_burn"),
        "uid_registration_min_burn_tao": _rao_to_tao(hyperparameters.get("min_burn")),
        "uid_registration_max_burn_rao": hyperparameters.get("max_burn"),
        "uid_registration_max_burn_tao": _rao_to_tao(hyperparameters.get("max_burn")),
        "target_registrations_per_interval": hyperparameters.get("target_regs_per_interval"),
        "max_registrations_per_block": hyperparameters.get("max_regs_per_block"),
        "uid_miner_immunity_period_blocks": hyperparameters.get("immunity_period"),
        "uid_miner_immunity_note": (
            "This hyperparameter protects newly registered UIDs/miners; "
            "it is not the subnet's lifecycle immunity."
        ),
        "activity_cutoff_blocks": hyperparameters.get("activity_cutoff"),
        "tempo_blocks": hyperparameters.get("tempo", getattr(subnet, "tempo", "N/A")),
        "blocks_since_last_step": getattr(subnet, "blocks_since_last_step", "N/A"),
        "last_step_block": getattr(subnet, "last_step", "N/A"),
        "network_registered_at_block": getattr(subnet, "network_registered_at", "N/A"),
        "difficulty": hyperparameters.get("difficulty"),
        "min_difficulty": hyperparameters.get("min_difficulty"),
        "max_difficulty": hyperparameters.get("max_difficulty"),
        "weights_version": hyperparameters.get("weights_version"),
        "weights_rate_limit_blocks": hyperparameters.get("weights_rate_limit"),
        "min_allowed_weights": hyperparameters.get("min_allowed_weights"),
        "max_weight_limit": hyperparameters.get("max_weight_limit"),
        "max_validators": hyperparameters.get("max_validators"),
        "kappa": hyperparameters.get("kappa"),
        "rho": hyperparameters.get("rho"),
        "yuma_version": hyperparameters.get("yuma_version"),
        "commit_reveal_period": hyperparameters.get("commit_reveal_period"),
        "commit_reveal_weights_enabled": hyperparameters.get("commit_reveal_weights_enabled"),
        "liquid_alpha_enabled": hyperparameters.get("liquid_alpha_enabled"),
        "alpha_low": hyperparameters.get("alpha_low"),
        "alpha_high": hyperparameters.get("alpha_high"),
        "alpha_sigmoid_steepness": hyperparameters.get("alpha_sigmoid_steepness"),
        "bonds_moving_avg": hyperparameters.get("bonds_moving_avg"),
        "bonds_reset_enabled": hyperparameters.get("bonds_reset_enabled"),
        "subnet_is_active": hyperparameters.get("subnet_is_active"),
        "transfers_enabled": hyperparameters.get("transfers_enabled"),
        "serving_rate_limit": hyperparameters.get("serving_rate_limit"),
        "user_liquidity_enabled": hyperparameters.get("user_liquidity_enabled"),
        "subnet_owner_hotkey": getattr(subnet, "owner_hotkey", "N/A"),
        "subnet_owner_coldkey": getattr(subnet, "owner_coldkey", "N/A"),
        "subnet_symbol": getattr(subnet, "symbol", "N/A"),
        "subnet_price": str(getattr(subnet, "price", "N/A")),
        "subnet_moving_price": getattr(subnet, "moving_price", "N/A"),
        "alpha_in": str(getattr(subnet, "alpha_in", "N/A")),
        "alpha_out": str(getattr(subnet, "alpha_out", "N/A")),
        "tao_in": str(getattr(subnet, "tao_in", "N/A")),
        "pending_alpha_emission": str(getattr(subnet, "pending_alpha_emission", "N/A")),
        "pending_root_emission": str(getattr(subnet, "pending_root_emission", "N/A")),
        "subnet_volume": str(getattr(subnet, "subnet_volume", "N/A")),
    }


def _uid_info_from_metagraph(meta, uid: int) -> dict:
    n_neurons = int(meta.n.item()) if hasattr(meta.n, "item") else int(meta.n)
    if uid >= n_neurons:
        return {"uid": uid, "error": f"UID {uid} does not exist on subnet {NETUID}"}

    incentives = meta.I.tolist()
    ranked = sorted(enumerate(incentives), key=lambda x: x[1], reverse=True)
    rank_map = {u: r + 1 for r, (u, _) in enumerate(ranked)}
    result = {
        "uid": uid,
        "chain_rank": rank_map.get(uid, "N/A"),
        "chain_incentive": round(float(meta.I[uid]), 6),
        "stake": round(float(meta.S[uid]), 4),
        "hotkey": meta.hotkeys[uid],
        "coldkey": meta.coldkeys[uid],
        "active": bool(meta.active[uid]),
        "validator_permit": bool(meta.validator_permit[uid]),
        "axon_is_serving": bool(getattr(meta.axons[uid], "is_serving", False)),
    }
    trust = _metagraph_float(meta, uid, "T", "trust", "validator_trust")
    if trust is not None:
        result["trust"] = trust
    return result


def _top_miner_from_metagraph(meta) -> dict | None:
    n_neurons = int(meta.n.item()) if hasattr(meta.n, "item") else int(meta.n)
    if n_neurons <= 0:
        return None
    incentives = meta.I.tolist()
    uid = max(range(n_neurons), key=lambda i: float(incentives[i]))
    info = _uid_info_from_metagraph(meta, uid)
    info["changed_by"] = "chain_incentive"
    return info


def _role_counts_from_metagraph(meta) -> dict:
    n_neurons = int(meta.n.item()) if hasattr(meta.n, "item") else int(meta.n)
    active = [bool(value) for value in meta.active]
    permits = [bool(value) for value in meta.validator_permit]
    serving = [bool(getattr(axon, "is_serving", False)) for axon in meta.axons]
    incentives = [float(value) for value in meta.I]
    dividends = [float(value) for value in meta.D]
    return {
        "registered_validators": sum(permits),
        "registered_miners": n_neurons - sum(permits),
        "active_validators": sum(value > 0 for value in dividends),
        "active_miners": sum(value > 0 for value in incentives),
        "active_dual_uids": sum(
            incentive > 0 and dividend > 0
            for incentive, dividend in zip(incentives, dividends)
        ),
        "raw_metagraph_active_validators": sum(
            is_active and has_permit
            for is_active, has_permit in zip(active, permits)
        ),
        "raw_metagraph_active_miners": sum(
            is_active and not has_permit
            for is_active, has_permit in zip(active, permits)
        ),
        "serving_validators": sum(
            is_serving and has_permit
            for is_serving, has_permit in zip(serving, permits)
        ),
        "serving_miners": sum(
            is_serving and not has_permit
            for is_serving, has_permit in zip(serving, permits)
        ),
    }


def _fetch_subnet_info() -> dict:
    with _subtensor_client() as sub:
        meta = sub.metagraph(NETUID)

        subnet = sub.all_subnets()[NETUID]
        hyperparameters = sub.get_subnet_hyperparameters(NETUID)
        serialized_hyperparameters = _serialize_hyperparameters(hyperparameters)
        current_block = int(sub.get_current_block())
        subnet_lifecycle = _subnet_lifecycle(
            current_block,
            getattr(subnet, "network_registered_at", None),
        )
        current_burn_rao = int(sub.get_hyperparameter("Burn", NETUID) or 0)
        registrations_this_interval = int(
            sub.get_hyperparameter("RegistrationsThisInterval", NETUID) or 0
        )
        burn_registrations_this_interval = int(
            sub.get_hyperparameter("BurnRegistrationsThisInterval", NETUID) or 0
        )
        recycle_metrics = _update_recycle_metrics(
            block=current_block,
            current_burn_tao=current_burn_rao / 1_000_000_000,
            registrations_this_interval=registrations_this_interval,
            burn_registrations_this_interval=burn_registrations_this_interval,
            target_regs_per_interval=int(
                serialized_hyperparameters.get("target_regs_per_interval") or 0
            ),
            adjustment_interval=int(
                serialized_hyperparameters.get("adjustment_interval") or 0
            ),
        )
        try:
            subnet_registration_cost = sub.get_subnet_burn_cost()
        except Exception as e:
            logger.error("Subnet registration cost fetch error: %s", e)
            subnet_registration_cost = None

        incentive_burn = 0.0

        for i in range(len(meta.uids)):
            if meta.coldkeys[i] == subnet.owner_coldkey:
                incentive_burn += float(meta.I[i])
        burn_rate = round(incentive_burn, 6)

        n_neurons = int(meta.n.item()) if hasattr(meta.n, "item") else int(meta.n)
        data = {
            "netuid": NETUID,
            "name": getattr(subnet, "subnet_name", f"Subnet {NETUID}"),
            "n_neurons": n_neurons,
            "total_stake": round(float(meta.S.sum()), 4),
            "tempo": getattr(subnet, "tempo", "N/A"),
            "emission": _subnet_float(subnet, "tao_in_emission", "emission_value") * 100 / 0.5,
            "incentive_burn": burn_rate,
            "burn_rate": burn_rate,
            "owner_coldkey": subnet.owner_coldkey,
            "owner_hotkey": getattr(subnet, "owner_hotkey", "N/A"),
            "top_miner": _top_miner_from_metagraph(meta),
            "role_counts": _role_counts_from_metagraph(meta),
            "recycle_metrics": recycle_metrics,
            "subnet_lifecycle": subnet_lifecycle,
            "hyperparameters": serialized_hyperparameters,
            "detailed_parameters": _detailed_parameters(
                subnet,
                serialized_hyperparameters,
                subnet_registration_cost,
            ),
            "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }
        try:
            data["alpha_price"] = str(sub.get_subnet_price(NETUID))
        except Exception:
            data["alpha_price"] = "N/A"
        return data


def get_subnet_info(force: bool = False) -> dict:
    cached = _get("subnet_info", CACHE_TTL_PRICE)
    if cached and not force:
        return cached
    try:
        data = _fetch_subnet_info()
        _set("subnet_info", data)
        return data
    except Exception as e:
        logger.error(f"Chain subnet fetch error: {e}")
        if cached:
            return cached
        return {"error": str(e)}


def refresh_subnet_info() -> dict:
    data = get_subnet_info(force=True)
    if "error" not in data:
        logger.info(
            "Bittensor chain cache updated: netuid=%s name=%s emission=%s alpha_price=%s",
            data.get("netuid"),
            data.get("name"),
            data.get("emission"),
            data.get("alpha_price"),
        )
    return data


def get_uid_info(uid: int) -> dict:
    key = f"uid_{uid}"
    cached = _get(key, CACHE_TTL_MINERS)
    if cached:
        return cached
    try:
        with _subtensor_client() as sub:
            meta = sub.metagraph(NETUID)
            result = _uid_info_from_metagraph(meta, uid)
        _set(key, result)
        return result
    except Exception as e:
        logger.error(f"Chain UID fetch error: {e}")
        return {"error": str(e)}


def get_uids_info(uids: list[int]) -> dict[int, dict]:
    result = {}
    missing = []
    for uid in dict.fromkeys(uids):
        cached = _get(f"uid_{uid}", CACHE_TTL_MINERS)
        if cached:
            result[uid] = cached
        else:
            missing.append(uid)

    if not missing:
        return result

    try:
        with _subtensor_client() as sub:
            meta = sub.metagraph(NETUID)
            for uid in missing:
                info = _uid_info_from_metagraph(meta, uid)
                _set(f"uid_{uid}", info)
                result[uid] = info
    except Exception as e:
        logger.error(f"Chain UIDs fetch error: {e}")
        for uid in missing:
            result[uid] = {"uid": uid, "error": str(e)}

    return result


def get_all_uid_infos() -> list[dict]:
    try:
        with _subtensor_client() as sub:
            meta = sub.metagraph(NETUID)
            n_neurons = int(meta.n.item()) if hasattr(meta.n, "item") else int(meta.n)
            infos = []
            for uid in range(n_neurons):
                info = _uid_info_from_metagraph(meta, uid)
                _set(f"uid_{uid}", info)
                infos.append(info)
            return infos
    except Exception as e:
        logger.error(f"Chain all UIDs fetch error: {e}")
        return [{"error": str(e)}]


def build_live_data_block() -> str:
    info = get_subnet_info()
    now = info.get("fetched_at") or datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"[Chain data as of {now}]"]

    if "error" not in info:
        lines.append(f"Subnet: {info.get('name')} (netuid {info.get('netuid')})")
        lines.append(f"Alpha price: {info.get('alpha_price')}")
        lines.append(f"Total neurons: {info.get('n_neurons')}")
        lines.append(f"Total stake: {info.get('total_stake')} TAO")
        lines.append(f"Emission: {info.get('emission')}%")
        lines.append(f"Incentive burn: {info.get('incentive_burn')}")
        lines.append(f"Burn rate: {info.get('burn_rate')}")
        lines.append(f"Owner hotkey: {info.get('owner_hotkey')}")
        lines.append(f"Owner coldkey: {info.get('owner_coldkey')}")
        if info.get("role_counts"):
            roles = info["role_counts"]
            lines.append(
                "Validator/miner counts: "
                f"registered_validators={roles.get('registered_validators')}, "
                f"registered_miners={roles.get('registered_miners')}, "
                f"active_validators={roles.get('active_validators')} (dividends > 0), "
                f"active_miners={roles.get('active_miners')} (incentive > 0), "
                f"active_dual_uids={roles.get('active_dual_uids')}, "
                f"raw_metagraph_active_validators={roles.get('raw_metagraph_active_validators')}, "
                f"raw_metagraph_active_miners={roles.get('raw_metagraph_active_miners')}, "
                f"serving_validators={roles.get('serving_validators')}, "
                f"serving_miners={roles.get('serving_miners')}"
            )
        if info.get("recycle_metrics"):
            recycle = info["recycle_metrics"]
            lines.append(
                "Registration recycle metrics: "
                f"current_recycle_cost_tao={recycle.get('current_recycle_cost_tao')}, "
                f"registrations_this_interval={recycle.get('registrations_this_interval')}, "
                f"burn_registrations_this_interval={recycle.get('burn_registrations_this_interval')}, "
                f"observed_registrations_24h={recycle.get('observed_registrations_24h')}, "
                f"observed_recycle_24h_tao_estimate={recycle.get('observed_recycle_24h_tao_estimate')}, "
                f"projected_registrations_per_day={recycle.get('projected_registrations_per_day')}, "
                f"projected_recycle_per_day_tao={recycle.get('projected_recycle_per_day_tao')}. "
                f"{recycle.get('calculation_note')}"
            )
        if info.get("subnet_lifecycle"):
            lifecycle = info["subnet_lifecycle"]
            lines.append(
                "Subnet lifecycle: "
                f"registered_at_block={lifecycle.get('network_registered_at_block')}, "
                f"registration_time_utc_estimate={lifecycle.get('registration_time_utc_estimate')}, "
                f"age_blocks={lifecycle.get('subnet_age_blocks')}, "
                f"age_days_estimate={lifecycle.get('subnet_age_days_estimate')}, "
                f"subnet_immunity_policy_months={lifecycle.get('subnet_immunity_policy_months')}, "
                f"subnet_immunity_end_utc_estimate={lifecycle.get('subnet_immunity_end_utc_estimate')}, "
                f"subnet_immunity_status_estimate={lifecycle.get('subnet_immunity_status_estimate')}. "
                f"{lifecycle.get('calculation_note')}"
            )
        if info.get("top_miner"):
            top = info["top_miner"]
            lines.append(
                "Top miner by on-chain incentive: "
                f"UID {top.get('uid')} | rank={top.get('chain_rank')} | "
                f"incentive={top.get('chain_incentive')} | stake={top.get('stake')} | "
                f"hotkey={top.get('hotkey')} | coldkey={top.get('coldkey')}"
            )
        if info.get("detailed_parameters"):
            lines.append("Detailed subnet parameters:")
            for key, value in info["detailed_parameters"].items():
                lines.append(f"  {key}: {value}")
        if info.get("hyperparameters"):
            lines.append("Raw subnet hyperparameters:")
            for key, value in info["hyperparameters"].items():
                lines.append(f"  {key}: {value}")
        # lines.append(f"Tempo: {info.get('tempo')}")
    else:
        lines.append(f"Chain unavailable: {info['error']}")
    return "\n".join(lines)
