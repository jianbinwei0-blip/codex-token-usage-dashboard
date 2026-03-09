from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .models import DailyTotals
from .pricing import PricingCatalog


DEFAULT_MODEL = "unknown"


def safe_non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def normalized_bucket_value(value: object, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return fallback


def apply_usage_to_daily(
    daily: DailyTotals,
    *,
    agent_cli: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    total_tokens: int,
    input_cost_usd: float,
    output_cost_usd: float,
    cached_cost_usd: float,
    total_cost_usd: float,
    cost_complete: bool,
) -> None:
    daily.add_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        input_cost_usd=input_cost_usd,
        output_cost_usd=output_cost_usd,
        cached_cost_usd=cached_cost_usd,
        total_cost_usd=total_cost_usd,
        cost_complete=cost_complete,
    )
    daily.add_breakdown(
        agent_cli=agent_cli,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        input_cost_usd=input_cost_usd,
        output_cost_usd=output_cost_usd,
        cached_cost_usd=cached_cost_usd,
        total_cost_usd=total_cost_usd,
        cost_complete=cost_complete,
    )


def iter_jsonl_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            if filename.endswith(".jsonl"):
                yield Path(dirpath) / filename


def parse_codex_session_usage(session_path: Path) -> dict[str, int | str] | None:
    latest_usage: dict[str, int] | None = None
    latest_model = DEFAULT_MODEL
    agent_cli = "codex"
    session_id = session_path.stem

    with session_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            if event_type == "session_meta":
                payload = event.get("payload") or {}
                if isinstance(payload, dict):
                    session_id = normalized_bucket_value(payload.get("id"), session_id)
                    agent_cli = normalized_bucket_value(payload.get("originator"), "")
                    if not agent_cli:
                        agent_cli = normalized_bucket_value(payload.get("source"), "codex")
                continue

            if event_type == "turn_context":
                payload = event.get("payload") or {}
                if isinstance(payload, dict):
                    latest_model = normalized_bucket_value(payload.get("model"), latest_model)
                continue

            if event_type != "event_msg":
                continue

            payload = event.get("payload") or {}
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue

            info = payload.get("info") or {}
            if not isinstance(info, dict):
                continue

            total_usage = info.get("total_token_usage") or {}
            if not isinstance(total_usage, dict):
                continue

            input_tokens = safe_non_negative_int(total_usage.get("input_tokens"))
            cached_tokens = safe_non_negative_int(total_usage.get("cached_input_tokens"))
            output_tokens = safe_non_negative_int(total_usage.get("output_tokens"))
            total_tokens = safe_non_negative_int(total_usage.get("total_tokens"))
            if total_tokens == 0 and (input_tokens or cached_tokens or output_tokens):
                total_tokens = input_tokens + output_tokens

            if total_tokens == 0 and input_tokens == 0 and cached_tokens == 0 and output_tokens == 0:
                continue

            latest_usage = {
                "input_tokens": input_tokens,
                "cached_tokens": cached_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }

    if latest_usage is None:
        return None

    return {
        "session_id": session_id,
        "agent_cli": normalized_bucket_value(agent_cli, "codex"),
        "model": normalized_bucket_value(latest_model, DEFAULT_MODEL),
        **latest_usage,
    }


def collect_codex_daily_totals(
    sessions_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> dict[dt.date, DailyTotals]:
    totals: dict[dt.date, DailyTotals] = {}
    if not sessions_root.exists():
        return totals

    catalog = pricing_catalog or PricingCatalog.from_file(None)
    bucket_sessions: dict[tuple[dt.date, str, str], set[str]] = {}

    for file_path in iter_jsonl_files(sessions_root):
        relative = file_path.relative_to(sessions_root).parts
        if len(relative) < 4:
            continue
        try:
            year = int(relative[0])
            month = int(relative[1])
            day = int(relative[2])
            usage_date = dt.date(year, month, day)
        except ValueError:
            continue

        usage = parse_codex_session_usage(file_path)
        if usage is None:
            continue

        session_id = normalized_bucket_value(usage.get("session_id"), file_path.stem)
        agent_cli = normalized_bucket_value(usage.get("agent_cli"), "codex")
        model = normalized_bucket_value(usage.get("model"), DEFAULT_MODEL)
        input_tokens = safe_non_negative_int(usage.get("input_tokens"))
        output_tokens = safe_non_negative_int(usage.get("output_tokens"))
        cached_tokens = safe_non_negative_int(usage.get("cached_tokens"))
        total_tokens = safe_non_negative_int(usage.get("total_tokens"))
        priced = catalog.price_usage(
            "codex",
            model,
            uncached_input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached_tokens,
        )

        daily = totals.setdefault(usage_date, DailyTotals(date=usage_date))
        daily.sessions += 1
        apply_usage_to_daily(
            daily,
            agent_cli=agent_cli,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            input_cost_usd=priced.input_cost_usd,
            output_cost_usd=priced.output_cost_usd,
            cached_cost_usd=priced.cached_cost_usd,
            total_cost_usd=priced.total_cost_usd,
            cost_complete=priced.cost_complete,
        )
        bucket_sessions.setdefault((usage_date, agent_cli, model), set()).add(session_id)

    for (usage_date, agent_cli, model), sessions in bucket_sessions.items():
        daily = totals.get(usage_date)
        if daily is not None:
            daily.add_breakdown(agent_cli=agent_cli, model=model, sessions=len(sessions))

    return totals


def parse_timestamp_local(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)

    return parsed.astimezone()


def collect_claude_daily_totals(
    claude_projects_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> dict[dt.date, DailyTotals]:
    totals: dict[dt.date, DailyTotals] = {}
    if not claude_projects_root.exists():
        return totals

    catalog = pricing_catalog or PricingCatalog.from_file(None)
    request_usage: dict[tuple[str, str], dict[str, object]] = {}

    for file_path in iter_jsonl_files(claude_projects_root):
        session_scope = file_path.stem
        with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                request_id = event.get("requestId")
                if not isinstance(request_id, str) or not request_id:
                    continue

                local_timestamp = parse_timestamp_local(event.get("timestamp"))
                if local_timestamp is None:
                    continue

                message = event.get("message") or {}
                if not isinstance(message, dict):
                    continue

                usage = message.get("usage") or {}
                if not isinstance(usage, dict):
                    continue

                session_id = event.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    session_id = session_scope

                dedupe_key = (session_id, request_id)
                current = request_usage.get(dedupe_key)

                input_tokens = safe_non_negative_int(usage.get("input_tokens"))
                cache_creation_input_tokens = safe_non_negative_int(usage.get("cache_creation_input_tokens"))
                cache_read_input_tokens = safe_non_negative_int(usage.get("cache_read_input_tokens"))
                output_tokens = safe_non_negative_int(usage.get("output_tokens"))
                cached_tokens = cache_creation_input_tokens + cache_read_input_tokens
                model = normalized_bucket_value(message.get("model"), DEFAULT_MODEL)

                if current is None:
                    request_usage[dedupe_key] = {
                        "session_id": session_id,
                        "timestamp": local_timestamp,
                        "input_tokens": input_tokens,
                        "cache_creation_input_tokens": cache_creation_input_tokens,
                        "cache_read_input_tokens": cache_read_input_tokens,
                        "cached_tokens": cached_tokens,
                        "output_tokens": output_tokens,
                        "model": model,
                    }
                    continue

                current["timestamp"] = max(current["timestamp"], local_timestamp)
                current["input_tokens"] = max(safe_non_negative_int(current.get("input_tokens")), input_tokens)
                current["cache_creation_input_tokens"] = max(
                    safe_non_negative_int(current.get("cache_creation_input_tokens")),
                    cache_creation_input_tokens,
                )
                current["cache_read_input_tokens"] = max(
                    safe_non_negative_int(current.get("cache_read_input_tokens")),
                    cache_read_input_tokens,
                )
                current["cached_tokens"] = max(safe_non_negative_int(current.get("cached_tokens")), cached_tokens)
                current["output_tokens"] = max(safe_non_negative_int(current.get("output_tokens")), output_tokens)
                if model != DEFAULT_MODEL:
                    current["model"] = model

    daily_sessions: dict[dt.date, set[str]] = {}
    bucket_sessions: dict[tuple[dt.date, str, str], set[str]] = {}

    for request in request_usage.values():
        timestamp = request["timestamp"]
        if not isinstance(timestamp, dt.datetime):
            continue

        usage_date = timestamp.date()
        daily = totals.setdefault(usage_date, DailyTotals(date=usage_date))
        input_tokens = safe_non_negative_int(request.get("input_tokens"))
        cache_creation_input_tokens = safe_non_negative_int(request.get("cache_creation_input_tokens"))
        cache_read_input_tokens = safe_non_negative_int(request.get("cache_read_input_tokens"))
        cached_tokens = safe_non_negative_int(request.get("cached_tokens"))
        output_tokens = safe_non_negative_int(request.get("output_tokens"))
        request_total = input_tokens + cached_tokens + output_tokens
        agent_cli = "claude-code"
        model = normalized_bucket_value(request.get("model"), DEFAULT_MODEL)
        priced = catalog.price_usage(
            "claude",
            model,
            uncached_input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_input_tokens,
            cache_write_tokens=cache_creation_input_tokens,
        )

        apply_usage_to_daily(
            daily,
            agent_cli=agent_cli,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=request_total,
            input_cost_usd=priced.input_cost_usd,
            output_cost_usd=priced.output_cost_usd,
            cached_cost_usd=priced.cached_cost_usd,
            total_cost_usd=priced.total_cost_usd,
            cost_complete=priced.cost_complete,
        )

        session_id = normalized_bucket_value(request.get("session_id"), "unknown-session")
        daily_sessions.setdefault(usage_date, set()).add(session_id)
        bucket_sessions.setdefault((usage_date, agent_cli, model), set()).add(session_id)

    for usage_date, sessions in daily_sessions.items():
        if usage_date in totals:
            totals[usage_date].sessions = len(sessions)

    for (usage_date, agent_cli, model), sessions in bucket_sessions.items():
        daily = totals.get(usage_date)
        if daily is not None:
            daily.add_breakdown(agent_cli=agent_cli, model=model, sessions=len(sessions))

    return totals


def collect_pi_daily_totals(
    pi_agent_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> dict[dt.date, DailyTotals]:
    totals: dict[dt.date, DailyTotals] = {}
    if not pi_agent_root.exists():
        return totals

    sessions_root = pi_agent_root / "sessions"
    if not sessions_root.exists():
        return totals

    catalog = pricing_catalog or PricingCatalog.from_file(None)
    daily_sessions: dict[dt.date, set[str]] = {}
    bucket_sessions: dict[tuple[dt.date, str, str], set[str]] = {}

    for file_path in iter_jsonl_files(sessions_root):
        session_id = file_path.stem
        active_model = DEFAULT_MODEL
        with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                if event_type == "session":
                    event_session_id = event.get("id")
                    if isinstance(event_session_id, str) and event_session_id:
                        session_id = event_session_id
                    continue

                if event_type == "model_change":
                    active_model = normalized_bucket_value(event.get("modelId"), active_model)
                    continue

                if event_type != "message":
                    continue

                local_timestamp = parse_timestamp_local(event.get("timestamp"))
                if local_timestamp is None:
                    continue

                message = event.get("message") or {}
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue

                usage = message.get("usage") or {}
                if not isinstance(usage, dict):
                    continue

                usage_date = local_timestamp.date()
                input_tokens = safe_non_negative_int(usage.get("input"))
                output_tokens = safe_non_negative_int(usage.get("output"))
                cache_read_tokens = safe_non_negative_int(usage.get("cacheRead"))
                cache_write_tokens = safe_non_negative_int(usage.get("cacheWrite"))
                cached_tokens = cache_read_tokens + cache_write_tokens
                total_tokens = safe_non_negative_int(usage.get("totalTokens"))
                if total_tokens == 0 and (input_tokens or output_tokens or cached_tokens):
                    total_tokens = input_tokens + output_tokens + cached_tokens

                native_cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else None
                model = normalized_bucket_value(message.get("model"), active_model)
                priced = catalog.price_usage(
                    "pi",
                    model,
                    uncached_input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    native_cost=native_cost,
                )

                daily = totals.setdefault(usage_date, DailyTotals(date=usage_date))
                agent_cli = "pi"
                apply_usage_to_daily(
                    daily,
                    agent_cli=agent_cli,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    total_tokens=total_tokens,
                    input_cost_usd=priced.input_cost_usd,
                    output_cost_usd=priced.output_cost_usd,
                    cached_cost_usd=priced.cached_cost_usd,
                    total_cost_usd=priced.total_cost_usd,
                    cost_complete=priced.cost_complete,
                )
                daily_sessions.setdefault(usage_date, set()).add(session_id)
                bucket_sessions.setdefault((usage_date, agent_cli, model), set()).add(session_id)

    for usage_date, sessions in daily_sessions.items():
        if usage_date in totals:
            totals[usage_date].sessions = len(sessions)

    for (usage_date, agent_cli, model), sessions in bucket_sessions.items():
        daily = totals.get(usage_date)
        if daily is not None:
            daily.add_breakdown(agent_cli=agent_cli, model=model, sessions=len(sessions))

    return totals
