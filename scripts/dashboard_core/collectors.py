from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from .models import ActivityTotals, DailyTotals
from .pricing import PricingCatalog


DEFAULT_MODEL = "unknown"
LOCAL_TIMEZONE = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
CODEX_ROLLOUT_TIMESTAMP_PATTERN = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})"
)
_CODEX_SESSION_USAGE_CACHE: dict[str, tuple[int, int, dict[str, int | str | dt.datetime] | None]] = {}
_CLAUDE_REQUEST_RECORDS_CACHE: dict[str, tuple[int, int, list[dict[str, object]]]] = {}
_PI_SESSION_RECORDS_CACHE: dict[str, tuple[int, int, tuple[str, list[dict[str, object]]]]] = {}


def safe_non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def normalized_bucket_value(value: object, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return fallback


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


def parse_codex_rollout_timestamp_local(session_path: Path) -> dt.datetime | None:
    match = CODEX_ROLLOUT_TIMESTAMP_PATTERN.search(session_path.stem)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=LOCAL_TIMEZONE)


def codex_usage_date_from_path(session_path: Path, sessions_root: Path) -> dt.date | None:
    relative = session_path.relative_to(sessions_root).parts
    if len(relative) < 4:
        return None
    try:
        year = int(relative[0])
        month = int(relative[1])
        day = int(relative[2])
        return dt.date(year, month, day)
    except ValueError:
        return None


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


def add_usage_to_activity(
    activity_totals: dict[tuple[dt.date, int], ActivityTotals],
    timestamp: dt.datetime,
    *,
    sessions: int,
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
    key = (timestamp.date(), timestamp.hour)
    activity = activity_totals.get(key)
    if activity is None:
        activity = ActivityTotals(date=timestamp.date(), hour=timestamp.hour)
        activity_totals[key] = activity
    activity.add_usage(
        sessions=sessions,
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


def parse_codex_session_usage(session_path: Path) -> dict[str, int | str | dt.datetime] | None:
    latest_usage: dict[str, int] | None = None
    latest_model = DEFAULT_MODEL
    agent_cli = "codex"
    session_id = session_path.stem
    activity_timestamp = parse_codex_rollout_timestamp_local(session_path)

    with session_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_timestamp = parse_timestamp_local(event.get("timestamp"))
            if activity_timestamp is None and event_timestamp is not None:
                activity_timestamp = event_timestamp

            event_type = event.get("type")

            if event_type == "session_meta":
                payload = event.get("payload") or {}
                if isinstance(payload, dict):
                    session_id = normalized_bucket_value(payload.get("id"), session_id)
                    agent_cli = normalized_bucket_value(payload.get("originator"), "")
                    if not agent_cli:
                        agent_cli = normalized_bucket_value(payload.get("source"), "codex")
                    payload_timestamp = parse_timestamp_local(payload.get("timestamp"))
                    if payload_timestamp is not None:
                        activity_timestamp = payload_timestamp
                    elif activity_timestamp is None and event_timestamp is not None:
                        activity_timestamp = event_timestamp
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

            if activity_timestamp is None and event_timestamp is not None:
                activity_timestamp = event_timestamp

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
        "timestamp": activity_timestamp,
        **latest_usage,
    }


def parse_codex_session_usage_cached(session_path: Path) -> dict[str, int | str | dt.datetime] | None:
    try:
        stat = session_path.stat()
    except OSError:
        return None

    cache_key = str(session_path)
    cached = _CODEX_SESSION_USAGE_CACHE.get(cache_key)
    signature = (stat.st_size, stat.st_mtime_ns)
    if cached is not None and cached[:2] == signature:
        return cached[2]

    usage = parse_codex_session_usage(session_path)
    _CODEX_SESSION_USAGE_CACHE[cache_key] = (signature[0], signature[1], usage)
    return usage


def collect_codex_usage_data(
    sessions_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> tuple[dict[dt.date, DailyTotals], dict[tuple[dt.date, int], ActivityTotals]]:
    totals: dict[dt.date, DailyTotals] = {}
    activity_totals: dict[tuple[dt.date, int], ActivityTotals] = {}
    if not sessions_root.exists():
        return totals, activity_totals

    catalog = pricing_catalog or PricingCatalog.from_file(None)
    bucket_sessions: dict[tuple[dt.date, str, str], set[str]] = {}

    for file_path in iter_jsonl_files(sessions_root):
        fallback_usage_date = codex_usage_date_from_path(file_path, sessions_root)
        if fallback_usage_date is None:
            continue

        usage = parse_codex_session_usage_cached(file_path)
        if usage is None:
            continue

        activity_timestamp = usage.get("timestamp") if isinstance(usage.get("timestamp"), dt.datetime) else None
        usage_date = activity_timestamp.date() if activity_timestamp is not None else fallback_usage_date
        if activity_timestamp is None:
            activity_timestamp = dt.datetime.combine(usage_date, dt.time(hour=0), tzinfo=LOCAL_TIMEZONE)

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
        add_usage_to_activity(
            activity_totals,
            activity_timestamp,
            sessions=1,
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

    for (usage_date, agent_cli, model), sessions in bucket_sessions.items():
        daily = totals.get(usage_date)
        if daily is not None:
            daily.add_breakdown(agent_cli=agent_cli, model=model, sessions=len(sessions))

    return totals, activity_totals


def collect_codex_daily_totals(
    sessions_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> dict[dt.date, DailyTotals]:
    totals, _activity_totals = collect_codex_usage_data(sessions_root, pricing_catalog=pricing_catalog)
    return totals


def parse_claude_request_records(file_path: Path) -> list[dict[str, object]]:
    session_scope = file_path.stem
    records: list[dict[str, object]] = []

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

            input_tokens = safe_non_negative_int(usage.get("input_tokens"))
            cache_creation_input_tokens = safe_non_negative_int(usage.get("cache_creation_input_tokens"))
            cache_read_input_tokens = safe_non_negative_int(usage.get("cache_read_input_tokens"))
            output_tokens = safe_non_negative_int(usage.get("output_tokens"))
            cached_tokens = cache_creation_input_tokens + cache_read_input_tokens
            model = normalized_bucket_value(message.get("model"), DEFAULT_MODEL)

            records.append(
                {
                    "session_id": session_id,
                    "request_id": request_id,
                    "timestamp": local_timestamp,
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens,
                    "cached_tokens": cached_tokens,
                    "output_tokens": output_tokens,
                    "model": model,
                }
            )

    return records


def parse_claude_request_records_cached(file_path: Path) -> list[dict[str, object]]:
    try:
        stat = file_path.stat()
    except OSError:
        return []

    cache_key = str(file_path)
    cached = _CLAUDE_REQUEST_RECORDS_CACHE.get(cache_key)
    signature = (stat.st_size, stat.st_mtime_ns)
    if cached is not None and cached[:2] == signature:
        return cached[2]

    records = parse_claude_request_records(file_path)
    _CLAUDE_REQUEST_RECORDS_CACHE[cache_key] = (signature[0], signature[1], records)
    return records


def collect_claude_usage_data(
    claude_projects_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> tuple[dict[dt.date, DailyTotals], dict[tuple[dt.date, int], ActivityTotals]]:
    totals: dict[dt.date, DailyTotals] = {}
    activity_totals: dict[tuple[dt.date, int], ActivityTotals] = {}
    if not claude_projects_root.exists():
        return totals, activity_totals

    catalog = pricing_catalog or PricingCatalog.from_file(None)
    request_usage: dict[tuple[str, str], dict[str, object]] = {}

    for file_path in iter_jsonl_files(claude_projects_root):
        for record in parse_claude_request_records_cached(file_path):
            session_id = normalized_bucket_value(record.get("session_id"), file_path.stem)
            request_id = normalized_bucket_value(record.get("request_id"), "")
            if not request_id:
                continue

            dedupe_key = (session_id, request_id)
            current = request_usage.get(dedupe_key)
            local_timestamp = record.get("timestamp")
            if not isinstance(local_timestamp, dt.datetime):
                continue

            input_tokens = safe_non_negative_int(record.get("input_tokens"))
            cache_creation_input_tokens = safe_non_negative_int(record.get("cache_creation_input_tokens"))
            cache_read_input_tokens = safe_non_negative_int(record.get("cache_read_input_tokens"))
            cached_tokens = safe_non_negative_int(record.get("cached_tokens"))
            output_tokens = safe_non_negative_int(record.get("output_tokens"))
            model = normalized_bucket_value(record.get("model"), DEFAULT_MODEL)

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
    daily_session_usage: dict[tuple[dt.date, str], dict[str, object]] = {}

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

        session_key = (usage_date, session_id)
        session_activity = daily_session_usage.get(session_key)
        if session_activity is None:
            daily_session_usage[session_key] = {
                "timestamp": timestamp,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": request_total,
                "input_cost_usd": priced.input_cost_usd,
                "output_cost_usd": priced.output_cost_usd,
                "cached_cost_usd": priced.cached_cost_usd,
                "total_cost_usd": priced.total_cost_usd,
                "cost_complete": priced.cost_complete,
            }
        else:
            session_activity["timestamp"] = min(session_activity["timestamp"], timestamp)
            session_activity["input_tokens"] = safe_non_negative_int(session_activity.get("input_tokens")) + input_tokens
            session_activity["output_tokens"] = safe_non_negative_int(session_activity.get("output_tokens")) + output_tokens
            session_activity["cached_tokens"] = safe_non_negative_int(session_activity.get("cached_tokens")) + cached_tokens
            session_activity["total_tokens"] = safe_non_negative_int(session_activity.get("total_tokens")) + request_total
            session_activity["input_cost_usd"] = float(session_activity.get("input_cost_usd") or 0.0) + priced.input_cost_usd
            session_activity["output_cost_usd"] = float(session_activity.get("output_cost_usd") or 0.0) + priced.output_cost_usd
            session_activity["cached_cost_usd"] = float(session_activity.get("cached_cost_usd") or 0.0) + priced.cached_cost_usd
            session_activity["total_cost_usd"] = float(session_activity.get("total_cost_usd") or 0.0) + priced.total_cost_usd
            session_activity["cost_complete"] = bool(session_activity.get("cost_complete", True)) and priced.cost_complete

    for usage_date, sessions in daily_sessions.items():
        if usage_date in totals:
            totals[usage_date].sessions = len(sessions)

    for (usage_date, agent_cli, model), sessions in bucket_sessions.items():
        daily = totals.get(usage_date)
        if daily is not None:
            daily.add_breakdown(agent_cli=agent_cli, model=model, sessions=len(sessions))

    for session_activity in daily_session_usage.values():
        timestamp = session_activity.get("timestamp")
        if not isinstance(timestamp, dt.datetime):
            continue
        add_usage_to_activity(
            activity_totals,
            timestamp,
            sessions=1,
            input_tokens=safe_non_negative_int(session_activity.get("input_tokens")),
            output_tokens=safe_non_negative_int(session_activity.get("output_tokens")),
            cached_tokens=safe_non_negative_int(session_activity.get("cached_tokens")),
            total_tokens=safe_non_negative_int(session_activity.get("total_tokens")),
            input_cost_usd=float(session_activity.get("input_cost_usd") or 0.0),
            output_cost_usd=float(session_activity.get("output_cost_usd") or 0.0),
            cached_cost_usd=float(session_activity.get("cached_cost_usd") or 0.0),
            total_cost_usd=float(session_activity.get("total_cost_usd") or 0.0),
            cost_complete=bool(session_activity.get("cost_complete", True)),
        )

    return totals, activity_totals


def collect_claude_daily_totals(
    claude_projects_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> dict[dt.date, DailyTotals]:
    totals, _activity_totals = collect_claude_usage_data(claude_projects_root, pricing_catalog=pricing_catalog)
    return totals


def parse_pi_session_records(file_path: Path) -> tuple[str, list[dict[str, object]]]:
    session_id = file_path.stem
    active_model = DEFAULT_MODEL
    records: list[dict[str, object]] = []

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

            input_tokens = safe_non_negative_int(usage.get("input"))
            output_tokens = safe_non_negative_int(usage.get("output"))
            cache_read_tokens = safe_non_negative_int(usage.get("cacheRead"))
            cache_write_tokens = safe_non_negative_int(usage.get("cacheWrite"))
            cached_tokens = cache_read_tokens + cache_write_tokens
            total_tokens = safe_non_negative_int(usage.get("totalTokens"))
            if total_tokens == 0 and (input_tokens or output_tokens or cached_tokens):
                total_tokens = input_tokens + output_tokens + cached_tokens

            records.append(
                {
                    "timestamp": local_timestamp,
                    "model": normalized_bucket_value(message.get("model"), active_model),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "cached_tokens": cached_tokens,
                    "total_tokens": total_tokens,
                    "native_cost": usage.get("cost") if isinstance(usage.get("cost"), dict) else None,
                }
            )

    return session_id, records


def parse_pi_session_records_cached(file_path: Path) -> tuple[str, list[dict[str, object]]]:
    try:
        stat = file_path.stat()
    except OSError:
        return file_path.stem, []

    cache_key = str(file_path)
    cached = _PI_SESSION_RECORDS_CACHE.get(cache_key)
    signature = (stat.st_size, stat.st_mtime_ns)
    if cached is not None and cached[:2] == signature:
        return cached[2]

    records = parse_pi_session_records(file_path)
    _PI_SESSION_RECORDS_CACHE[cache_key] = (signature[0], signature[1], records)
    return records


def collect_pi_usage_data(
    pi_agent_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> tuple[dict[dt.date, DailyTotals], dict[tuple[dt.date, int], ActivityTotals]]:
    totals: dict[dt.date, DailyTotals] = {}
    activity_totals: dict[tuple[dt.date, int], ActivityTotals] = {}
    if not pi_agent_root.exists():
        return totals, activity_totals

    sessions_root = pi_agent_root / "sessions"
    if not sessions_root.exists():
        return totals, activity_totals

    catalog = pricing_catalog or PricingCatalog.from_file(None)
    daily_sessions: dict[dt.date, set[str]] = {}
    bucket_sessions: dict[tuple[dt.date, str, str], set[str]] = {}
    daily_session_usage: dict[tuple[dt.date, str], dict[str, object]] = {}

    for file_path in iter_jsonl_files(sessions_root):
        session_id, records = parse_pi_session_records_cached(file_path)
        for record in records:
            local_timestamp = record.get("timestamp")
            if not isinstance(local_timestamp, dt.datetime):
                continue

            usage_date = local_timestamp.date()
            input_tokens = safe_non_negative_int(record.get("input_tokens"))
            output_tokens = safe_non_negative_int(record.get("output_tokens"))
            cache_read_tokens = safe_non_negative_int(record.get("cache_read_tokens"))
            cache_write_tokens = safe_non_negative_int(record.get("cache_write_tokens"))
            cached_tokens = safe_non_negative_int(record.get("cached_tokens"))
            total_tokens = safe_non_negative_int(record.get("total_tokens"))
            native_cost = record.get("native_cost") if isinstance(record.get("native_cost"), dict) else None
            model = normalized_bucket_value(record.get("model"), DEFAULT_MODEL)
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

            session_key = (usage_date, session_id)
            session_activity = daily_session_usage.get(session_key)
            if session_activity is None:
                daily_session_usage[session_key] = {
                    "timestamp": local_timestamp,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cached_tokens": cached_tokens,
                    "total_tokens": total_tokens,
                    "input_cost_usd": priced.input_cost_usd,
                    "output_cost_usd": priced.output_cost_usd,
                    "cached_cost_usd": priced.cached_cost_usd,
                    "total_cost_usd": priced.total_cost_usd,
                    "cost_complete": priced.cost_complete,
                }
            else:
                session_activity["timestamp"] = min(session_activity["timestamp"], local_timestamp)
                session_activity["input_tokens"] = safe_non_negative_int(session_activity.get("input_tokens")) + input_tokens
                session_activity["output_tokens"] = safe_non_negative_int(session_activity.get("output_tokens")) + output_tokens
                session_activity["cached_tokens"] = safe_non_negative_int(session_activity.get("cached_tokens")) + cached_tokens
                session_activity["total_tokens"] = safe_non_negative_int(session_activity.get("total_tokens")) + total_tokens
                session_activity["input_cost_usd"] = float(session_activity.get("input_cost_usd") or 0.0) + priced.input_cost_usd
                session_activity["output_cost_usd"] = float(session_activity.get("output_cost_usd") or 0.0) + priced.output_cost_usd
                session_activity["cached_cost_usd"] = float(session_activity.get("cached_cost_usd") or 0.0) + priced.cached_cost_usd
                session_activity["total_cost_usd"] = float(session_activity.get("total_cost_usd") or 0.0) + priced.total_cost_usd
                session_activity["cost_complete"] = bool(session_activity.get("cost_complete", True)) and priced.cost_complete

    for usage_date, sessions in daily_sessions.items():
        if usage_date in totals:
            totals[usage_date].sessions = len(sessions)

    for (usage_date, agent_cli, model), sessions in bucket_sessions.items():
        daily = totals.get(usage_date)
        if daily is not None:
            daily.add_breakdown(agent_cli=agent_cli, model=model, sessions=len(sessions))

    for session_activity in daily_session_usage.values():
        timestamp = session_activity.get("timestamp")
        if not isinstance(timestamp, dt.datetime):
            continue
        add_usage_to_activity(
            activity_totals,
            timestamp,
            sessions=1,
            input_tokens=safe_non_negative_int(session_activity.get("input_tokens")),
            output_tokens=safe_non_negative_int(session_activity.get("output_tokens")),
            cached_tokens=safe_non_negative_int(session_activity.get("cached_tokens")),
            total_tokens=safe_non_negative_int(session_activity.get("total_tokens")),
            input_cost_usd=float(session_activity.get("input_cost_usd") or 0.0),
            output_cost_usd=float(session_activity.get("output_cost_usd") or 0.0),
            cached_cost_usd=float(session_activity.get("cached_cost_usd") or 0.0),
            total_cost_usd=float(session_activity.get("total_cost_usd") or 0.0),
            cost_complete=bool(session_activity.get("cost_complete", True)),
        )

    return totals, activity_totals


def collect_pi_daily_totals(
    pi_agent_root: Path,
    pricing_catalog: PricingCatalog | None = None,
) -> dict[dt.date, DailyTotals]:
    totals, _activity_totals = collect_pi_usage_data(pi_agent_root, pricing_catalog=pricing_catalog)
    return totals
