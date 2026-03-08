from __future__ import annotations

import datetime as dt

from .aggregation import (
    breakdown_rows_from_daily,
    combine_daily_totals,
    current_week_end,
    providers_available,
    rows_from_daily,
    slice_daily,
    summary_from_daily,
    sum_range,
)
from .collectors import collect_claude_daily_totals, collect_codex_daily_totals, collect_pi_daily_totals
from .config import DashboardConfig
from .models import round_cost
from .pricing import PricingCatalog
from .render import build_breakdown_table_body, build_stats_section, build_table_body, rewrite_dashboard_html


def recalc_dashboard(config: DashboardConfig, now: dt.datetime | None = None) -> dict:
    now_utc = now.astimezone(dt.timezone.utc) if now is not None else dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone()
    today = now_local.date()
    ytd_from = dt.date(today.year, 1, 1)
    pricing_catalog = PricingCatalog.from_file(config.pricing_file)

    codex_daily_all = collect_codex_daily_totals(config.sessions_root, pricing_catalog=pricing_catalog)
    claude_daily_all = collect_claude_daily_totals(config.claude_projects_root, pricing_catalog=pricing_catalog)
    pi_daily_all = collect_pi_daily_totals(config.pi_agent_root, pricing_catalog=pricing_catalog)
    combined_daily_all = combine_daily_totals(codex_daily_all, claude_daily_all, pi_daily_all)

    codex_daily_ytd = slice_daily(codex_daily_all, ytd_from, today)
    claude_daily_ytd = slice_daily(claude_daily_all, ytd_from, today)
    pi_daily_ytd = slice_daily(pi_daily_all, ytd_from, today)
    combined_daily_ytd = slice_daily(combined_daily_all, ytd_from, today)

    rows = sorted(combined_daily_ytd.values(), key=lambda item: (-item.total_tokens, item.date.isoformat()))
    days_count = len(rows)
    sessions_total = sum(item.sessions for item in rows)
    input_total = sum(item.input_tokens for item in rows)
    output_total = sum(item.output_tokens for item in rows)
    cached_total = sum(item.cached_tokens for item in rows)
    ytd_total = sum(item.total_tokens for item in rows)
    input_cost_total = round_cost(sum(item.input_cost_usd for item in rows))
    output_cost_total = round_cost(sum(item.output_cost_usd for item in rows))
    cached_cost_total = round_cost(sum(item.cached_cost_usd for item in rows))
    total_cost = round_cost(sum(item.total_cost_usd for item in rows))
    cost_complete = all(item.cost_complete for item in rows) if rows else True
    highest = rows[0].total_tokens if rows else 0

    today_sessions, today_total = sum_range(combined_daily_all, today, today)

    current_monday = today - dt.timedelta(days=today.isoweekday() - 1)
    week_end = current_week_end(today)
    current_week_sessions, current_week_total = sum_range(combined_daily_all, current_monday, week_end)

    prev_week_monday = current_monday - dt.timedelta(days=7)
    prev_week_sunday = current_monday - dt.timedelta(days=1)
    prev_week_sessions, prev_week_total = sum_range(combined_daily_all, prev_week_monday, prev_week_sunday)

    prev2_week_monday = prev_week_monday - dt.timedelta(days=7)
    prev2_week_sunday = prev_week_monday - dt.timedelta(days=1)
    prev2_week_sessions, prev2_week_total = sum_range(combined_daily_all, prev2_week_monday, prev2_week_sunday)

    stats_section = build_stats_section(
        today=today,
        ytd_total=ytd_total,
        days_count=days_count,
        sessions_total=sessions_total,
        highest=highest,
        input_total=input_total,
        output_total=output_total,
        cached_total=cached_total,
        total_cost=total_cost,
        input_cost_total=input_cost_total,
        output_cost_total=output_cost_total,
        cached_cost_total=cached_cost_total,
        cost_complete=cost_complete,
        today_sessions=today_sessions,
        today_total=today_total,
        current_monday=current_monday,
        current_week_end=week_end,
        current_week_sessions=current_week_sessions,
        current_week_total=current_week_total,
        prev_week_monday=prev_week_monday,
        prev_week_sunday=prev_week_sunday,
        prev_week_sessions=prev_week_sessions,
        prev_week_total=prev_week_total,
        prev2_week_monday=prev2_week_monday,
        prev2_week_sunday=prev2_week_sunday,
        prev2_week_sessions=prev2_week_sessions,
        prev2_week_total=prev2_week_total,
    )

    table_body = build_table_body(rows)
    breakdown_body = build_breakdown_table_body(breakdown_rows_from_daily(combined_daily_ytd))

    codex_rows_all = rows_from_daily(codex_daily_all)
    claude_rows_all = rows_from_daily(claude_daily_all)
    pi_rows_all = rows_from_daily(pi_daily_all)
    combined_rows_all = rows_from_daily(combined_daily_all)
    provider_flags = providers_available(
        config.sessions_root.exists(),
        config.claude_projects_root.exists(),
        config.pi_agent_root.exists(),
    )
    pricing_metadata = pricing_catalog.metadata()

    dataset_payload = {
        "generated_at": now_utc.isoformat(),
        "timezone": now_local.tzname() or "local",
        "paths": {
            "codex_sessions_root": str(config.sessions_root),
            "claude_projects_root": str(config.claude_projects_root),
            "pi_agent_root": str(config.pi_agent_root),
            "pi_sessions_root": str(config.pi_agent_root / "sessions"),
            "pricing_file": str(config.pricing_file) if config.pricing_file else None,
        },
        "providers_available": provider_flags,
        "pricing": pricing_metadata,
        "providers": {
            "codex": {"rows": codex_rows_all},
            "claude": {"rows": claude_rows_all},
            "pi": {"rows": pi_rows_all},
            "combined": {"rows": combined_rows_all},
        },
    }

    html = config.dashboard_html.read_text(encoding="utf-8")
    html = rewrite_dashboard_html(html, stats_section, table_body, breakdown_body, dataset_payload)
    config.dashboard_html.write_text(html, encoding="utf-8")

    return {
        "ok": True,
        "updated_at": now_utc.isoformat(),
        "today": today.isoformat(),
        "input_tokens": input_total,
        "output_tokens": output_total,
        "cached_tokens": cached_total,
        "ytd_total_tokens": ytd_total,
        "input_cost_usd": input_cost_total,
        "output_cost_usd": output_cost_total,
        "cached_cost_usd": cached_cost_total,
        "total_cost_usd": total_cost,
        "cost_complete": cost_complete,
        "cost_status": "complete" if cost_complete else "partial",
        "days_with_usage": days_count,
        "sessions": sessions_total,
        "sources": {
            "codex_sessions_root": str(config.sessions_root),
            "claude_projects_root": str(config.claude_projects_root),
            "pi_agent_root": str(config.pi_agent_root),
            "pricing_file": str(config.pricing_file) if config.pricing_file else None,
        },
        "providers_available": provider_flags,
        "pricing": pricing_metadata,
        "providers": {
            "codex": summary_from_daily(codex_daily_ytd),
            "claude": summary_from_daily(claude_daily_ytd),
            "pi": summary_from_daily(pi_daily_ytd),
            "combined": summary_from_daily(combined_daily_ytd),
        },
    }
