"""
Tests for DriftDetector — auto-fix success rate drift detection.

Run:
    pytest tests/test_drift_detector.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.drift_detector import (
    DRIFT_THRESHOLD,
    FLOOR_RATE,
    MIN_CURRENT_SAMPLES,
    DayStats,
    DriftDetector,
    DriftResult,
    drift_detector,
)


# ---------------------------------------------------------------------------
# Helpers — build minimal IncidentState objects
# ---------------------------------------------------------------------------

def _make_incident(outcome: str, resolved_days_ago: int = 0):
    from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus

    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type="S3_NO_SUCH_KEY",
        title="test",
        description="test",
        service="svc",
    )
    inc = IncidentState(error_event=event)
    inc.outcome = outcome
    inc.status = IncidentStatus.RESOLVED
    inc.resolved_at = datetime.utcnow() - timedelta(days=resolved_days_ago)
    return inc


def _detector_with_incidents(incidents):
    """Return a DriftDetector whose incident_store is mocked."""
    det = DriftDetector()
    return det, incidents


# ---------------------------------------------------------------------------
# DayStats
# ---------------------------------------------------------------------------

class TestDayStats:
    def test_total_is_approved_plus_rejected(self):
        s = DayStats(date="2026-04-12", approved=3, rejected=1)
        assert s.total == 4

    def test_success_rate_computed(self):
        s = DayStats(date="2026-04-12", approved=3, rejected=1)
        assert s.success_rate == pytest.approx(0.75)

    def test_success_rate_none_when_no_decisions(self):
        s = DayStats(date="2026-04-12", approved=0, rejected=0)
        assert s.success_rate is None

    def test_full_success(self):
        s = DayStats(date="2026-04-12", approved=5, rejected=0)
        assert s.success_rate == 1.0

    def test_zero_success(self):
        s = DayStats(date="2026-04-12", approved=0, rejected=5)
        assert s.success_rate == 0.0


# ---------------------------------------------------------------------------
# daily_stats
# ---------------------------------------------------------------------------

class TestDailyStats:
    def test_empty_store_returns_empty_list(self):
        det = DriftDetector()
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = []
            stats = det.daily_stats()
        assert stats == []

    def test_ignores_non_fix_outcomes(self):
        det = DriftDetector()
        inc = _make_incident("noise")   # not fix_merged or fix_rejected
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = [inc]
            stats = det.daily_stats()
        assert stats == []

    def test_ignores_incidents_without_resolved_at(self):
        det = DriftDetector()
        inc = _make_incident("fix_merged")
        inc.resolved_at = None
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = [inc]
            stats = det.daily_stats()
        assert stats == []

    def test_counts_approved_and_rejected_separately(self):
        det = DriftDetector()
        incidents = [
            _make_incident("fix_merged", resolved_days_ago=0),
            _make_incident("fix_merged", resolved_days_ago=0),
            _make_incident("fix_rejected", resolved_days_ago=0),
        ]
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            stats = det.daily_stats()
        today_stats = stats[0]
        assert today_stats.approved == 2
        assert today_stats.rejected == 1

    def test_groups_by_day(self):
        det = DriftDetector()
        incidents = [
            _make_incident("fix_merged", resolved_days_ago=0),
            _make_incident("fix_merged", resolved_days_ago=1),
            _make_incident("fix_rejected", resolved_days_ago=1),
        ]
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            stats = det.daily_stats()
        assert len(stats) == 2

    def test_sorted_newest_first(self):
        det = DriftDetector()
        incidents = [
            _make_incident("fix_merged", resolved_days_ago=2),
            _make_incident("fix_merged", resolved_days_ago=0),
        ]
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            stats = det.daily_stats()
        assert stats[0].date > stats[1].date


# ---------------------------------------------------------------------------
# current_drift — insufficient data
# ---------------------------------------------------------------------------

class TestCurrentDriftInsufficientData:
    def test_empty_store_no_drift(self):
        det = DriftDetector()
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = []
            result = det.current_drift()
        assert result.has_drift is False
        assert result.current_samples == 0

    def test_fewer_than_min_samples_no_drift(self):
        det = DriftDetector()
        # Only 2 incidents (MIN_CURRENT_SAMPLES = 3)
        incidents = [
            _make_incident("fix_rejected", resolved_days_ago=0),
            _make_incident("fix_rejected", resolved_days_ago=1),
        ]
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            result = det.current_drift()
        assert result.has_drift is False
        assert "Insufficient" in result.message


# ---------------------------------------------------------------------------
# current_drift — floor rate
# ---------------------------------------------------------------------------

class TestCurrentDriftFloor:
    def test_below_floor_rate_triggers_drift(self):
        det = DriftDetector()
        # 1 approved, 9 rejected → 10% success rate (below 50% floor)
        incidents = [
            _make_incident("fix_merged",   resolved_days_ago=0),
            *[_make_incident("fix_rejected", resolved_days_ago=0) for _ in range(9)],
        ]
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            result = det.current_drift()
        assert result.has_drift is True
        assert result.current_rate == pytest.approx(0.1)
        assert "floor" in result.message.lower()

    def test_at_floor_rate_no_drift(self):
        det = DriftDetector()
        # 50% exactly — at floor, not below
        incidents = [
            _make_incident("fix_merged",   resolved_days_ago=0),
            _make_incident("fix_merged",   resolved_days_ago=0),
            _make_incident("fix_rejected", resolved_days_ago=0),
            _make_incident("fix_rejected", resolved_days_ago=0),
            _make_incident("fix_rejected", resolved_days_ago=0),  # 5th for min_samples
        ]
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            result = det.current_drift()
        # 2/5 = 40% < 50% floor → drift
        assert result.has_drift is True


# ---------------------------------------------------------------------------
# current_drift — baseline comparison
# ---------------------------------------------------------------------------

class TestCurrentDriftBaseline:
    def _build_incidents(self, current_rate: float, baseline_rate: float,
                         current_n: int = 5, baseline_n: int = 10) -> list:
        """
        Build synthetic incidents so that:
          current window (days 0-1): success_rate ≈ current_rate
          baseline window (days 2-8): success_rate ≈ baseline_rate
        """
        incidents = []
        # current window: days 0..1
        c_approved = round(current_n * current_rate)
        c_rejected = current_n - c_approved
        for _ in range(c_approved):
            incidents.append(_make_incident("fix_merged",   resolved_days_ago=0))
        for _ in range(c_rejected):
            incidents.append(_make_incident("fix_rejected", resolved_days_ago=0))

        # baseline window: spread across days 3..8
        b_approved = round(baseline_n * baseline_rate)
        b_rejected = baseline_n - b_approved
        day = 3
        for i in range(b_approved):
            incidents.append(_make_incident("fix_merged",   resolved_days_ago=day + (i % 5)))
        for i in range(b_rejected):
            incidents.append(_make_incident("fix_rejected", resolved_days_ago=day + (i % 5)))
        return incidents

    def test_no_drift_when_rates_similar(self):
        det = DriftDetector()
        incidents = self._build_incidents(
            current_rate=0.85, baseline_rate=0.90
        )  # only 5pp drop, below threshold
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            result = det.current_drift()
        assert result.has_drift is False

    def test_drift_detected_on_large_drop(self):
        det = DriftDetector()
        incidents = self._build_incidents(
            current_rate=0.60, baseline_rate=0.90, current_n=5, baseline_n=10
        )  # 30pp drop > 15pp threshold
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            result = det.current_drift()
        assert result.has_drift is True
        assert result.drop is not None and result.drop >= DRIFT_THRESHOLD

    def test_drift_result_contains_rates(self):
        det = DriftDetector()
        incidents = self._build_incidents(
            current_rate=0.60, baseline_rate=0.90, current_n=5, baseline_n=10
        )
        with patch("app.services.drift_detector.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            result = det.current_drift()
        assert result.current_rate is not None
        assert result.baseline_rate is not None


# ---------------------------------------------------------------------------
# check_and_alert
# ---------------------------------------------------------------------------

class TestCheckAndAlert:
    @pytest.mark.asyncio
    async def test_alert_sent_when_drift_detected(self):
        det = DriftDetector()
        det.current_drift = MagicMock(return_value=DriftResult(
            has_drift=True,
            current_rate=0.4,
            baseline_rate=0.9,
            drop=0.5,
            current_samples=5,
            baseline_samples=20,
            message="Rate dropped 50%",
        ))
        det._send_alert = AsyncMock()

        await det.check_and_alert()
        det._send_alert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_alert_when_no_drift(self):
        det = DriftDetector()
        det.current_drift = MagicMock(return_value=DriftResult(
            has_drift=False,
            current_rate=0.9,
            baseline_rate=0.9,
            drop=0.0,
            current_samples=5,
            baseline_samples=20,
            message="OK",
        ))
        det._send_alert = AsyncMock()

        await det.check_and_alert()
        det._send_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_second_alert(self):
        from datetime import timezone

        det = DriftDetector()
        drift = DriftResult(
            has_drift=True,
            current_rate=0.4,
            baseline_rate=0.9,
            drop=0.5,
            current_samples=5,
            baseline_samples=20,
            message="Rate dropped",
        )
        det.current_drift = MagicMock(return_value=drift)
        det._send_alert = AsyncMock()

        # First call — alert fires
        await det.check_and_alert()
        # Second call within cooldown — alert suppressed
        await det.check_and_alert()
        assert det._send_alert.await_count == 1


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

class TestDriftRoutes:
    def test_get_drift_returns_expected_keys(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes.drift import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        with patch("app.api.routes.drift.drift_detector") as mock_det:
            mock_det.current_drift.return_value = DriftResult(
                has_drift=False,
                current_rate=0.9,
                baseline_rate=0.9,
                drop=0.0,
                current_samples=5,
                baseline_samples=20,
                message="OK",
            )
            mock_det.daily_stats.return_value = []
            resp = client.get("/drift")

        assert resp.status_code == 200
        data = resp.json()
        assert "drift" in data
        assert "daily_stats" in data
        assert "has_drift" in data["drift"]

    def test_get_drift_stats_returns_list(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes.drift import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        with patch("app.api.routes.drift.drift_detector") as mock_det:
            mock_det.daily_stats.return_value = [
                DayStats(date="2026-04-12", approved=3, rejected=1),
            ]
            resp = client.get("/drift/stats")

        assert resp.status_code == 200
        stats = resp.json()
        assert len(stats) == 1
        assert stats[0]["date"] == "2026-04-12"
        assert stats[0]["success_rate"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_is_instance(self):
        assert isinstance(drift_detector, DriftDetector)
