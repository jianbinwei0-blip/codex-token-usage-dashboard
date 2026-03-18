#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_core.config import DashboardConfig
from dashboard_core.pipeline import recalc_dashboard

REQUIRED_HTML_SNIPPETS = [
    'id="usageDataset"',
    'id="dailyUsageTableBody"',
    'id="usageBreakdownTableBody"',
]


def validate_payload(payload: dict, html: str) -> None:
    if not payload.get("ok"):
        raise AssertionError("recalc payload not ok")
    for snippet in REQUIRED_HTML_SNIPPETS:
        if snippet not in html:
            raise AssertionError(f"Rendered HTML missing required snippet: {snippet}")
    if payload.get("providers_available", {}).get("combined") is not True:
        raise AssertionError("combined provider unavailable")
    if payload.get("ytd_total_tokens", 0) <= 0:
        raise AssertionError("expected positive total tokens on live workload")


def run_benchmark(repeat: int) -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    config = DashboardConfig.from_env(repo_root)
    timings_ms: list[float] = []
    last_payload: dict | None = None

    for _ in range(repeat):
        started = time.perf_counter()
        payload = recalc_dashboard(config)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        html = config.dashboard_html.read_text(encoding="utf-8")
        validate_payload(payload, html)
        timings_ms.append(elapsed_ms)
        last_payload = payload

    assert last_payload is not None
    warm_timings = timings_ms[1:] if len(timings_ms) > 1 else timings_ms
    return {
        "ok": True,
        "repeat": repeat,
        "timings_ms": timings_ms,
        "median_ms": statistics.median(timings_ms),
        "warm_median_ms": statistics.median(warm_timings),
        "cold_ms": timings_ms[0],
        "min_ms": min(timings_ms),
        "max_ms": max(timings_ms),
        "payload": last_payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark live dashboard recalc on real local data")
    parser.add_argument("--repeat", type=int, default=5, help="Number of benchmark runs (default: 5)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    args = parser.parse_args()

    if args.repeat < 1:
        print("--repeat must be >= 1", file=sys.stderr)
        return 2

    try:
        result = run_benchmark(args.repeat)
    except Exception as exc:  # noqa: BLE001
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("benchmark: live recalc on local Codex/Claude/PI data")
    for idx, timing in enumerate(result["timings_ms"], start=1):
        print(f"run {idx}: {timing:.3f} ms")
    print(f"median_ms: {result['median_ms']:.3f}")
    print(f"warm_median_ms: {result['warm_median_ms']:.3f}")
    print(f"cold_ms: {result['cold_ms']:.3f}")
    print(f"min_ms: {result['min_ms']:.3f}")
    print(f"max_ms: {result['max_ms']:.3f}")
    print(f"METRIC live_recalc_ms={result['median_ms']:.6f}")
    print(f"METRIC cold_recalc_ms={result['cold_ms']:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
