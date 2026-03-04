from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class DailyTotals:
    date: dt.date
    sessions: int = 0
    total_tokens: int = 0
