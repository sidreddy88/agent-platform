"""
Tests for EvalRunner — triage-agent accuracy against the golden dataset.

Run:
    pytest tests/test_eval_runner.py -v
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.eval_runner import (
    ABResult,
    CaseResult,
    EvalCase,
    EvalRunResult,
    EvalRunner,
    _EvalAWSStub,
    _EvalStoreStub,
    _percentile,
    eval_runner,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_case(
    id: str = "eval_test",
    decision: str = "real",
    severity: list[str] | None = None,
) -> EvalCase:
    return EvalCase(
        id=id,
        description="test case",
        input={
            "error_type": "TEST_ERROR",
            "title": "Test error",
            "description": "Something went wrong",
            "service": "test-svc",
            "source": "application",
        },
        expected={
            "triage_decision": decision,
            "triage_severity": severity or ["P1", "P2"],
        },
        tags=["test"],
    )


def _make_case_result(
    passed: bool = True,
    decision_correct: bool = True,
    severity_correct: bool = True,
    duration_ms: int = 100,
) -> CaseResult:
    return CaseResult(
        case_id="eval_test",
        description="test case",
        model="claude-haiku-4-5-20251001",
        triage_decision="real",
        triage_severity="P1",
        expected_decision="real",
        expected_severity=["P1", "P2"],
        decision_correct=decision_correct,
        severity_correct=severity_correct,
        passed=passed,
        duration_ms=duration_ms,
    )


def _make_run_result(
    pass_rate: float = 0.8,
    p50_ms: float = 200.0,
    model: str = "claude-haiku-4-5-20251001",
) -> EvalRunResult:
    total = 10
    passed = int(total * pass_rate)
    return EvalRunResult(
        run_id="run_abc",
        model=model,
        timestamp="2026-04-12T00:00:00+00:00",
        total=total,
        passed=passed,
        failed=total - passed,
        pass_rate=pass_rate,
        p50_ms=p50_ms,
        p95_ms=p50_ms * 2,
        cases=[],
    )


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_p50_odd_list(self):
        assert _percentile([1, 2, 3, 4, 5], 50) == 3

    def test_p50_even_list(self):
        # ceil(4 * 50 / 100) - 1 = ceil(2) - 1 = 1  →  [10, 20, 30, 40][1] = 20
        assert _percentile([10, 20, 30, 40], 50) == 20

    def test_p95_returns_near_max(self):
        vals = list(range(1, 101))   # 1..100
        result = _percentile(vals, 95)
        assert result >= 95

    def test_p100_returns_max(self):
        vals = [5, 10, 15]
        assert _percentile(vals, 100) == 15

    def test_single_element(self):
        assert _percentile([42], 50) == 42

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            _percentile([], 50)


# ---------------------------------------------------------------------------
# EvalCase / dataset loading
# ---------------------------------------------------------------------------

class TestLoadDataset:
    def test_loads_valid_jsonl(self):
        cases_data = [
            {
                "id": "e001", "description": "test", "tags": ["a"],
                "input": {"error_type": "ERR", "title": "T", "description": "D", "service": "s"},
                "expected": {"triage_decision": "real", "triage_severity": ["P1"]},
            },
            {
                "id": "e002", "description": "test2", "tags": [],
                "input": {"error_type": "ERR2", "title": "T2", "description": "D2", "service": "s2"},
                "expected": {"triage_decision": "noise", "triage_severity": ["P3"]},
            },
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for c in cases_data:
                f.write(json.dumps(c) + "\n")
            tmp_path = Path(f.name)

        runner = EvalRunner()
        cases = runner.load_dataset(tmp_path)
        assert len(cases) == 2
        assert cases[0].id == "e001"
        assert cases[1].expected["triage_decision"] == "noise"
        tmp_path.unlink()

    def test_skips_blank_lines(self):
        raw = '{"id":"e1","description":"t","tags":[],"input":{"error_type":"E","title":"T","description":"D","service":"s"},"expected":{"triage_decision":"real","triage_severity":["P1"]}}\n\n'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(raw)
            tmp_path = Path(f.name)

        runner = EvalRunner()
        cases = runner.load_dataset(tmp_path)
        assert len(cases) == 1
        tmp_path.unlink()

    def test_raises_if_file_missing(self):
        runner = EvalRunner()
        with pytest.raises(FileNotFoundError):
            runner.load_dataset(Path("/nonexistent/dataset.jsonl"))

    def test_tags_default_empty(self):
        raw = '{"id":"e1","description":"t","input":{"error_type":"E","title":"T","description":"D","service":"s"},"expected":{"triage_decision":"real","triage_severity":["P1"]}}\n'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(raw)
            tmp_path = Path(f.name)

        runner = EvalRunner()
        cases = runner.load_dataset(tmp_path)
        assert cases[0].tags == []
        tmp_path.unlink()


# ---------------------------------------------------------------------------
# _EvalAWSStub / _EvalStoreStub
# ---------------------------------------------------------------------------

class TestEvalStubs:
    def test_aws_stub_occurrence_count(self):
        stub = _EvalAWSStub()
        assert stub.get_log_occurrences() == 5

    def test_aws_stub_cloudwatch_alarms_empty(self):
        stub = _EvalAWSStub()
        assert stub.get_cloudwatch_alarms() == []

    def test_aws_stub_catchall_returns_none(self):
        stub = _EvalAWSStub()
        result = stub.some_unknown_method(1, 2, key="val")
        assert result is None

    def test_store_stub_no_pr(self):
        stub = _EvalStoreStub()
        assert stub.get_pr_for_resource("anything") is None

    def test_store_stub_set_is_noop(self):
        stub = _EvalStoreStub()
        stub.set_pr_for_resource("key", "url")  # should not raise


# ---------------------------------------------------------------------------
# run_case
# ---------------------------------------------------------------------------

class TestRunCase:
    @pytest.mark.asyncio
    async def test_passing_case(self):
        from app.agents.triage import TriageResult

        runner = EvalRunner()
        case = _make_case(decision="real", severity=["P1", "P2"])

        mock_agent = MagicMock()
        mock_agent.triage = AsyncMock(return_value=TriageResult(
            decision="real",
            severity="P1",
            blast_radius="single_service",
            occurrences_24h=5,
            duplicate_pr=None,
            reasoning="test",
        ))

        with patch("app.services.eval_runner.TriageAgent", return_value=mock_agent), \
             patch("app.services.eval_runner.LLMService"):
            result = await runner.run_case(case, "claude-haiku-4-5-20251001")

        assert result.passed is True
        assert result.decision_correct is True
        assert result.severity_correct is True
        assert result.triage_decision == "real"
        assert result.triage_severity == "P1"

    @pytest.mark.asyncio
    async def test_wrong_decision_fails(self):
        from app.agents.triage import TriageResult

        runner = EvalRunner()
        case = _make_case(decision="real", severity=["P1", "P2"])

        mock_agent = MagicMock()
        mock_agent.triage = AsyncMock(return_value=TriageResult(
            decision="noise",  # wrong
            severity="P1",
            blast_radius="single_service",
            occurrences_24h=5,
            duplicate_pr=None,
            reasoning="test",
        ))

        with patch("app.services.eval_runner.TriageAgent", return_value=mock_agent), \
             patch("app.services.eval_runner.LLMService"):
            result = await runner.run_case(case, "claude-haiku-4-5-20251001")

        assert result.passed is False
        assert result.decision_correct is False

    @pytest.mark.asyncio
    async def test_wrong_severity_fails(self):
        from app.agents.triage import TriageResult

        runner = EvalRunner()
        case = _make_case(decision="real", severity=["P1", "P2"])

        mock_agent = MagicMock()
        mock_agent.triage = AsyncMock(return_value=TriageResult(
            decision="real",
            severity="P3",  # not in ["P1","P2"]
            blast_radius="single_service",
            occurrences_24h=5,
            duplicate_pr=None,
            reasoning="test",
        ))

        with patch("app.services.eval_runner.TriageAgent", return_value=mock_agent), \
             patch("app.services.eval_runner.LLMService"):
            result = await runner.run_case(case, "claude-haiku-4-5-20251001")

        assert result.passed is False
        assert result.severity_correct is False

    @pytest.mark.asyncio
    async def test_exception_captured_as_error(self):
        runner = EvalRunner()
        case = _make_case()

        mock_agent = MagicMock()
        mock_agent.triage = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        with patch("app.services.eval_runner.TriageAgent", return_value=mock_agent), \
             patch("app.services.eval_runner.LLMService"):
            result = await runner.run_case(case, "claude-haiku-4-5-20251001")

        assert result.passed is False
        assert result.error == "LLM timeout"
        assert result.triage_decision is None

    @pytest.mark.asyncio
    async def test_duration_recorded(self):
        from app.agents.triage import TriageResult

        runner = EvalRunner()
        case = _make_case()

        mock_agent = MagicMock()
        mock_agent.triage = AsyncMock(return_value=TriageResult(
            decision="real", severity="P1", blast_radius="single_service",
            occurrences_24h=5, duplicate_pr=None, reasoning="ok",
        ))

        with patch("app.services.eval_runner.TriageAgent", return_value=mock_agent), \
             patch("app.services.eval_runner.LLMService"):
            result = await runner.run_case(case, "claude-haiku-4-5-20251001")

        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_invalid_source_falls_back_to_application(self):
        """A case with an unknown source value should not raise."""
        from app.agents.triage import TriageResult

        runner = EvalRunner()
        case = EvalCase(
            id="x",
            description="invalid source",
            input={
                "error_type": "ERR",
                "title": "T",
                "description": "D",
                "service": "svc",
                "source": "totally_unknown_source",
            },
            expected={"triage_decision": "real", "triage_severity": ["P2"]},
            tags=[],
        )

        mock_agent = MagicMock()
        mock_agent.triage = AsyncMock(return_value=TriageResult(
            decision="real", severity="P2", blast_radius="unknown",
            occurrences_24h=0, duplicate_pr=None, reasoning="ok",
        ))

        with patch("app.services.eval_runner.TriageAgent", return_value=mock_agent), \
             patch("app.services.eval_runner.LLMService"):
            result = await runner.run_case(case, "claude-haiku-4-5-20251001")

        assert result.error is None


# ---------------------------------------------------------------------------
# run_dataset
# ---------------------------------------------------------------------------

class TestRunDataset:
    @pytest.mark.asyncio
    async def test_pass_rate_computed(self):
        runner = EvalRunner()
        cases = [_make_case(id=f"e{i}") for i in range(4)]

        results = [
            _make_case_result(passed=True, duration_ms=100),
            _make_case_result(passed=True, duration_ms=200),
            _make_case_result(passed=False, duration_ms=150),
            _make_case_result(passed=False, duration_ms=300),
        ]

        runner.run_case = AsyncMock(side_effect=results)
        run = await runner.run_dataset(model="claude-haiku-4-5-20251001", dataset=cases)

        assert run.total == 4
        assert run.passed == 2
        assert run.failed == 2
        assert run.pass_rate == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_latency_percentiles(self):
        runner = EvalRunner()
        cases = [_make_case(id=f"e{i}") for i in range(5)]

        results = [_make_case_result(duration_ms=ms) for ms in [100, 200, 300, 400, 500]]
        runner.run_case = AsyncMock(side_effect=results)

        run = await runner.run_dataset(model="claude-haiku-4-5-20251001", dataset=cases)

        assert run.p50_ms == 300.0
        assert run.p95_ms == 500.0

    @pytest.mark.asyncio
    async def test_empty_dataset_zero_pass_rate(self):
        runner = EvalRunner()
        run = await runner.run_dataset(model="claude-haiku-4-5-20251001", dataset=[])

        assert run.total == 0
        assert run.pass_rate == 0.0
        assert run.p50_ms is None
        assert run.p95_ms is None

    @pytest.mark.asyncio
    async def test_default_model_is_haiku(self):
        from app.services.llm import HAIKU_MODEL
        runner = EvalRunner()
        runner.run_case = AsyncMock(return_value=_make_case_result())

        run = await runner.run_dataset(dataset=[_make_case()])
        assert run.model == HAIKU_MODEL

    @pytest.mark.asyncio
    async def test_loads_dataset_when_not_provided(self):
        runner = EvalRunner()
        runner.load_dataset = MagicMock(return_value=[_make_case()])
        runner.run_case = AsyncMock(return_value=_make_case_result())

        await runner.run_dataset(model="model-x")
        runner.load_dataset.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_id_unique(self):
        runner = EvalRunner()
        runner.run_case = AsyncMock(return_value=_make_case_result())

        r1 = await runner.run_dataset(model="model-x", dataset=[_make_case()])
        r2 = await runner.run_dataset(model="model-x", dataset=[_make_case()])
        assert r1.run_id != r2.run_id


# ---------------------------------------------------------------------------
# run_ab
# ---------------------------------------------------------------------------

class TestRunAB:
    @pytest.mark.asyncio
    async def test_winner_model_b_on_higher_pass_rate(self):
        runner = EvalRunner()
        runner.run_dataset = AsyncMock(side_effect=[
            _make_run_result(pass_rate=0.70),   # model A
            _make_run_result(pass_rate=0.85),   # model B
        ])

        ab = await runner.run_ab(dataset=[_make_case()])
        assert ab.winner == "model_b"
        assert ab.pass_rate_delta == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_winner_model_a_on_higher_pass_rate(self):
        runner = EvalRunner()
        runner.run_dataset = AsyncMock(side_effect=[
            _make_run_result(pass_rate=0.90),   # model A
            _make_run_result(pass_rate=0.70),   # model B
        ])

        ab = await runner.run_ab(dataset=[_make_case()])
        assert ab.winner == "model_a"

    @pytest.mark.asyncio
    async def test_tie_when_delta_under_5pp(self):
        runner = EvalRunner()
        runner.run_dataset = AsyncMock(side_effect=[
            _make_run_result(pass_rate=0.80),
            _make_run_result(pass_rate=0.83),   # only 3pp diff
        ])

        ab = await runner.run_ab(dataset=[_make_case()])
        assert ab.winner == "tie"

    @pytest.mark.asyncio
    async def test_tie_at_exactly_5pp(self):
        runner = EvalRunner()
        runner.run_dataset = AsyncMock(side_effect=[
            _make_run_result(pass_rate=0.80),
            _make_run_result(pass_rate=0.85),   # exactly 5pp — still a tie
        ])

        ab = await runner.run_ab(dataset=[_make_case()])
        assert ab.winner == "tie"

    @pytest.mark.asyncio
    async def test_latency_delta_computed(self):
        runner = EvalRunner()
        runner.run_dataset = AsyncMock(side_effect=[
            _make_run_result(pass_rate=0.80, p50_ms=150.0),
            _make_run_result(pass_rate=0.85, p50_ms=300.0),
        ])

        ab = await runner.run_ab(dataset=[_make_case()])
        assert ab.latency_delta_ms == pytest.approx(150.0)

    @pytest.mark.asyncio
    async def test_default_models(self):
        from app.services.llm import HAIKU_MODEL, MODEL as SONNET_MODEL
        runner = EvalRunner()
        captured_models = []

        async def _fake_run_dataset(model=None, dataset=None):
            captured_models.append(model)
            return _make_run_result(model=model or "default")

        runner.run_dataset = _fake_run_dataset
        runner.load_dataset = MagicMock(return_value=[_make_case()])

        await runner.run_ab()
        assert HAIKU_MODEL in captured_models
        assert SONNET_MODEL in captured_models

    @pytest.mark.asyncio
    async def test_verdict_contains_model_names(self):
        runner = EvalRunner()
        runner.run_dataset = AsyncMock(side_effect=[
            _make_run_result(pass_rate=0.70, model="claude-haiku-4-5-20251001"),
            _make_run_result(pass_rate=0.85, model="claude-sonnet-4-6"),
        ])

        ab = await runner.run_ab(
            model_a="claude-haiku-4-5-20251001",
            model_b="claude-sonnet-4-6",
            dataset=[_make_case()],
        )
        # verdict should mention one of the model short-names
        assert "haiku" in ab.verdict.lower() or "sonnet" in ab.verdict.lower()


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

class TestEvalsRoutes:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes.evals import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_get_dataset_returns_cases(self):
        client = self._client()
        cases = [_make_case(id="ev001"), _make_case(id="ev002")]

        with patch("app.api.routes.evals.eval_runner") as mock_runner:
            mock_runner.load_dataset.return_value = cases
            resp = client.get("/evals/dataset")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == "ev001"

    def test_get_dataset_500_on_missing_file(self):
        client = self._client()

        with patch("app.api.routes.evals.eval_runner") as mock_runner:
            mock_runner.load_dataset.side_effect = FileNotFoundError("missing")
            resp = client.get("/evals/dataset")

        assert resp.status_code == 500

    def test_post_run_returns_summary(self):
        client = self._client()
        run = _make_run_result(pass_rate=0.9)
        run.cases = [_make_case_result()]

        with patch("app.api.routes.evals.eval_runner") as mock_runner:
            mock_runner.run_dataset = AsyncMock(return_value=run)
            resp = client.post("/evals/run", json={})

        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert data["summary"]["pass_rate"] == pytest.approx(0.9)
        assert "cases" in data

    def test_post_run_with_model_override(self):
        client = self._client()
        run = _make_run_result()
        run.cases = []

        with patch("app.api.routes.evals.eval_runner") as mock_runner:
            mock_runner.run_dataset = AsyncMock(return_value=run)
            resp = client.post("/evals/run", json={"model": "claude-sonnet-4-6"})

        assert resp.status_code == 200
        mock_runner.run_dataset.assert_awaited_once_with(model="claude-sonnet-4-6")

    def test_post_ab_returns_verdict(self):
        client = self._client()
        ab = ABResult(
            run_id="ab_xyz",
            timestamp="2026-04-12T00:00:00+00:00",
            model_a=_make_run_result(pass_rate=0.70),
            model_b=_make_run_result(pass_rate=0.85),
            pass_rate_delta=0.15,
            latency_delta_ms=100.0,
            winner="model_b",
            verdict="sonnet wins on accuracy (+15%).",
        )

        with patch("app.api.routes.evals.eval_runner") as mock_runner:
            mock_runner.run_ab = AsyncMock(return_value=ab)
            resp = client.post("/evals/ab", json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data["winner"] == "model_b"
        assert "verdict" in data
        assert "model_a" in data
        assert "model_b" in data

    def test_post_ab_500_on_missing_file(self):
        client = self._client()

        with patch("app.api.routes.evals.eval_runner") as mock_runner:
            mock_runner.run_ab = AsyncMock(side_effect=FileNotFoundError("missing"))
            resp = client.post("/evals/ab", json={})

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_is_instance(self):
        assert isinstance(eval_runner, EvalRunner)
