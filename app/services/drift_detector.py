"""
Drift detector — monitors the auto-fix success rate and alerts when it drops.

Success rate = human-approved fixes / (approved + rejected) per day.

Algorithm
---------
  baseline   7-day rolling average (days T-8 through T-2)
  current    average over the most recent 2 days
  drift      current_rate < baseline_rate - DRIFT_THRESHOLD (default 15pp)
             OR current_rate < FLOOR_RATE (absolute floor, default 50%)

Both conditions also require at least MIN_CURRENT_SAMPLES (default 3)
resolved incidents in the current window to suppress noise from days with
very few decisions.

The detector runs as a background task (check every 24 h) and fires a
Slack alert via alerting_service.  A 24 h cooldown prevents repeated
alerts for the same ongoing regression.

Usage
-----
    # Wired into app startup automatically — no manual calls needed.
    # To query on demand:
    from app.services.drift_detector import drift_detector
    result = drift_detector.current_drift()
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.services.incident_store import incident_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DRIFT_THRESHOLD: float = 0.15   # 15-percentage-point drop triggers an alert
FLOOR_RATE:      float = 0.50   # absolute floor — alert even with no baseline
MIN_CURRENT_SAMPLES: int = 3    # minimum decisions in current window to alert
BASELINE_DAYS:   int = 7        # how many days to use for the baseline
CURRENT_DAYS:    int = 2        # how many recent days form the "current" window
CHECK_INTERVAL:  int = 86_400   # seconds between checks (24 h)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DayStats:
    date: str                    # ISO format, e.g. "2026-04-12"
    approved: int
    rejected: int

    @property
    def total(self) -> int:
        return self.approved + self.rejected

    @property
    def success_rate(self) -> Optional[float]:
        return self.approved / self.total if self.total else None


@dataclass
class DriftResult:
    has_drift: bool
    current_rate: Optional[float]    # success rate in the current window
    baseline_rate: Optional[float]   # success rate in the baseline window
    drop: Optional[float]            # baseline − current (positive = regression)
    current_samples: int
    baseline_samples: int
    message: str


# ---------------------------------------------------------------------------
# DriftDetector
# ---------------------------------------------------------------------------

class DriftDetector:
    """
    Computes daily fix-success-rate stats and detects regressions.

    Reads resolved incidents directly from incident_store — no extra
    instrumentation needed.
    """

    def __init__(self) -> None:
        self._running = False
        self._last_alerted: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def daily_stats(self, n_days: int = 30) -> list[DayStats]:
        """
        Return per-day fix-decision stats for the most recent *n_days*.

        Only incidents with outcome "fix_merged" or "fix_rejected" and a
        resolved_at timestamp are counted.
        """
        cutoff = date.today() - timedelta(days=n_days)
        buckets: dict[str, DayStats] = {}

        for inc in incident_store.list_all():
            if inc.outcome not in ("fix_merged", "fix_rejected"):
                continue
            if not inc.resolved_at:
                continue
            day = inc.resolved_at.date() if hasattr(inc.resolved_at, "date") else None
            if day is None or day < cutoff:
                continue
            key = day.isoformat()
            if key not in buckets:
                buckets[key] = DayStats(date=key, approved=0, rejected=0)
            if inc.outcome == "fix_merged":
                buckets[key].approved += 1
            else:
                buckets[key].rejected += 1

        # Return sorted newest-first
        return sorted(buckets.values(), key=lambda s: s.date, reverse=True)

    def current_drift(
        self,
        *,
        baseline_days: int = BASELINE_DAYS,
        current_days: int = CURRENT_DAYS,
        min_current_samples: int = MIN_CURRENT_SAMPLES,
    ) -> DriftResult:
        """
        Compare the current window to the baseline and return a DriftResult.

        Windows (relative to today):
          current   last *current_days* days
          baseline  the *baseline_days* days before the current window
        """
        today = date.today()
        current_start = today - timedelta(days=current_days)
        baseline_end   = current_start
        baseline_start = baseline_end - timedelta(days=baseline_days)

        all_stats = {s.date: s for s in self.daily_stats(n_days=baseline_days + current_days + 1)}

        def _window(start: date, end: date) -> tuple[int, int]:
            approved = rejected = 0
            d = start
            while d < end:
                s = all_stats.get(d.isoformat())
                if s:
                    approved += s.approved
                    rejected += s.rejected
                d += timedelta(days=1)
            return approved, rejected

        cur_app, cur_rej = _window(current_start, today + timedelta(days=1))  # inclusive today
        bas_app, bas_rej = _window(baseline_start, baseline_end)

        cur_total = cur_app + cur_rej
        bas_total = bas_app + bas_rej

        cur_rate = cur_app / cur_total if cur_total else None
        bas_rate = bas_app / bas_total if bas_total else None

        if cur_total < min_current_samples:
            return DriftResult(
                has_drift=False,
                current_rate=cur_rate,
                baseline_rate=bas_rate,
                drop=None,
                current_samples=cur_total,
                baseline_samples=bas_total,
                message=f"Insufficient current-window data ({cur_total} < {min_current_samples} required)",
            )

        # Check absolute floor
        if cur_rate is not None and cur_rate < FLOOR_RATE:
            return DriftResult(
                has_drift=True,
                current_rate=cur_rate,
                baseline_rate=bas_rate,
                drop=(bas_rate - cur_rate) if bas_rate is not None else None,
                current_samples=cur_total,
                baseline_samples=bas_total,
                message=(
                    f"Success rate {cur_rate:.0%} is below the absolute floor "
                    f"of {FLOOR_RATE:.0%} ({cur_total} decisions in current window)"
                ),
            )

        # Check relative drop from baseline
        if bas_rate is not None and cur_rate is not None:
            drop = bas_rate - cur_rate
            if drop >= DRIFT_THRESHOLD:
                return DriftResult(
                    has_drift=True,
                    current_rate=cur_rate,
                    baseline_rate=bas_rate,
                    drop=drop,
                    current_samples=cur_total,
                    baseline_samples=bas_total,
                    message=(
                        f"Success rate dropped {drop:.0%} below baseline "
                        f"(current {cur_rate:.0%} vs baseline {bas_rate:.0%})"
                    ),
                )

        drop = (bas_rate - cur_rate) if (bas_rate is not None and cur_rate is not None) else None
        return DriftResult(
            has_drift=False,
            current_rate=cur_rate,
            baseline_rate=bas_rate,
            drop=drop,
            current_samples=cur_total,
            baseline_samples=bas_total,
            message="Success rate is within normal range",
        )

    async def check_and_alert(self) -> DriftResult:
        """
        Run drift check and fire a Slack alert if drift is detected.
        A 24 h cooldown prevents alert storms.
        """
        result = self.current_drift()

        if result.has_drift:
            now = datetime.now(timezone.utc)
            cooldown_elapsed = (
                self._last_alerted is None
                or (now - self._last_alerted).total_seconds() >= CHECK_INTERVAL
            )
            if cooldown_elapsed:
                await self._send_alert(result)
                self._last_alerted = now

        return result

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        self._running = True
        logger.info("[DriftDetector] Started (check interval %ds)", CHECK_INTERVAL)
        while self._running:
            try:
                result = await self.check_and_alert()
                if result.has_drift:
                    logger.warning("[DriftDetector] Drift detected: %s", result.message)
                else:
                    logger.debug("[DriftDetector] No drift: %s", result.message)
            except Exception as exc:
                logger.error("[DriftDetector] Check failed: %s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_alert(self, result: DriftResult) -> None:
        from app.services.alerting import Alert, Severity, alerting_service

        def pct(r):
            return f"{r:.0%}" if r is not None else "n/a"
        drop_str = f"{result.drop:.0%}" if result.drop is not None else "n/a"

        await alerting_service.send_alert(Alert(
            severity=Severity.ERROR,
            title="Auto-fix success rate regression detected",
            message=(
                f"*Current rate:* {pct(result.current_rate)} "
                f"({result.current_samples} decisions)\n"
                f"*Baseline rate:* {pct(result.baseline_rate)} "
                f"({result.baseline_samples} decisions)\n"
                f"*Drop:* {drop_str}\n"
                f"*Reason:* {result.message}\n\n"
                f"Review recent rejections at `/approvals` and check "
                f"`.preference_pairs.jsonl` for negative training examples."
            ),
            source="DriftDetector",
            metadata={
                "current_rate":      result.current_rate,
                "baseline_rate":     result.baseline_rate,
                "drop":              result.drop,
                "current_samples":   result.current_samples,
                "baseline_samples":  result.baseline_samples,
            },
        ))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

drift_detector = DriftDetector()
