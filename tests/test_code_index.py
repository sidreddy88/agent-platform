"""app.services.code_index: per-repo code index for DiagnosisAgent, shadow retrieval, and the DIAGNOSIS_RAG switch
in IncidentLoop. Fakes only: no embeddings, no clone, no Langfuse."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import code_index as ci


class FakeCollection:
    def __init__(self):
        self.n = 0

    def count(self):
        return self.n


class FakeRAG:
    def __init__(self, files=(), fail=False):
        self._collection = FakeCollection()
        self.files, self.fail, self.indexed = list(files), fail, []

    async def index_directory(self, path, root=None):
        if self.fail:
            raise RuntimeError("embedding API down")
        self.indexed.append((path, root))
        self._collection.n = 120
        return 120

    async def hybrid_search(self, query, n_results=4, min_score=0.45):
        return [SimpleNamespace(file_path=f) for f in self.files][:n_results]


class FakeRepo:
    local_path = "/tmp/clones/acme-api"

    def __init__(self):
        self.fresh = 0

    async def ensure_fresh(self):
        self.fresh += 1


def test_collection_name_is_per_repo_and_safe():
    assert ci.collection_name("VoyageCo", "Big-App.v2") == "code_voyageco_big_app_v2"


@pytest.mark.asyncio
async def test_refresh_indexes_the_clone_diagnosis_reads_and_marks_ready():
    repo, rag = FakeRepo(), FakeRAG()
    idx = ci.CodeIndex("o", "r", local_repo=repo, rag=rag)
    assert not idx.ready and idx.refresh_due()
    await idx.refresh()
    assert repo.fresh == 1 and rag.indexed == [(repo.local_path, repo.local_path)]
    assert idx.ready and not idx.refresh_due() and idx.status()["total_chunks"] == 120


@pytest.mark.asyncio
async def test_a_failed_refresh_never_raises_and_stays_not_ready():
    idx = ci.CodeIndex("o", "r", local_repo=FakeRepo(), rag=FakeRAG(fail=True))
    await idx.refresh()
    assert not idx.ready and "embedding API down" in idx.status()["last_error"]


@pytest.mark.asyncio
async def test_shadow_check_reports_whether_the_diagnosed_file_was_retrieved():
    idx = ci.CodeIndex("o", "r", local_repo=FakeRepo(),
                       rag=FakeRAG(files=["routes/api/users.js", "routes/api/users.js", "models/User.js"]))
    hit = await ci.RAGShadow(idx).check("TypeError in users", "./routes/api/users.js")
    assert hit["diagnosed_file_retrieved"] is True and hit["files"] == ["routes/api/users.js", "models/User.js"]
    miss = await ci.RAGShadow(idx).check("TypeError", "lib/other.js")
    assert miss["diagnosed_file_retrieved"] is False
    none = await ci.RAGShadow(idx).check("TypeError", None)
    assert none["diagnosed_file_retrieved"] is None             # no diagnosed file: nothing to score


def test_shadow_query_uses_the_incident_text():
    inc = SimpleNamespace(error_event=SimpleNamespace(title="CastError in api", error_type="CASTERROR",
                                                      description="Cast to ObjectId failed"))
    assert ci.shadow_query(inc) == "CastError in api\nCASTERROR\nCast to ObjectId failed"


# ---- IncidentLoop wiring -----------------------------------------------------------------------------------------

def loop_with(index, diagnosis_rag):
    from app.services.incident_loop import IncidentLoop
    loop = IncidentLoop.__new__(IncidentLoop)
    registered = []
    loop._diagnosis = SimpleNamespace(_rag=None, _register_tools=lambda: registered.append(True))
    loop._code_index = index
    return loop, registered


def test_diagnosis_gets_the_index_only_when_switched_on_and_ready(monkeypatch):
    from app.core.config import settings
    idx = ci.CodeIndex("o", "r", local_repo=FakeRepo(), rag=FakeRAG())
    idx.ready = True
    monkeypatch.setattr(settings, "diagnosis_rag", False)
    loop, registered = loop_with(idx, False)
    loop._attach_code_index()
    assert loop._diagnosis._rag is None and not registered        # shadow mode (the default)
    monkeypatch.setattr(settings, "diagnosis_rag", True)
    idx.ready = False
    loop._attach_code_index()
    assert loop._diagnosis._rag is None                            # on, but the index isn't built yet
    idx.ready = True
    loop._attach_code_index()
    loop._attach_code_index()
    assert loop._diagnosis._rag is idx.rag and registered == [True]   # attached once


@pytest.mark.asyncio
async def test_a_diagnosis_schedules_the_shadow_check_and_a_due_refresh(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "diagnosis_rag", False)
    rag = FakeRAG(files=["a.js"])
    idx = ci.CodeIndex("o", "r", local_repo=FakeRepo(), rag=rag)
    idx.ready, idx.last_refresh = True, None                      # ready but due for a refresh
    loop, _ = loop_with(idx, False)
    seen = []

    async def diagnose(incident, prior_context=None):
        return SimpleNamespace(affected_file="a.js")

    async def shadow(incident, result):
        seen.append(result.affected_file)
    loop._diagnosis.diagnose = diagnose
    loop._rag_shadow = shadow
    monkeypatch.setattr("app.services.incident_loop.handoff_validator.validate_diagnosis", lambda r: r)
    await loop._run_diagnosis(SimpleNamespace(id="i1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == ["a.js"] and rag.indexed                        # shadow ran; refresh ran in the background


def test_one_index_per_repo_is_shared_across_incident_loops(monkeypatch):
    monkeypatch.setattr(ci, "_SHARED", {})
    monkeypatch.setattr(ci, "CodeIndex", lambda owner, repo, local_repo=None: SimpleNamespace(owner=owner, repo=repo))
    a, b = ci.shared("o", "r"), ci.shared("o", "r")
    assert a is b and ci.shared("o", "other") is not a


@pytest.mark.asyncio
async def test_the_orchestrator_starts_the_index_build(monkeypatch):
    """Production runs Orchestrator.run_forever(), not IncidentLoop.run_forever(): the refresh must start there."""
    from app.services.orchestrator import MasterOrchestrator
    started = []

    async def refresh():
        started.append(True)
    orch = MasterOrchestrator.__new__(MasterOrchestrator)
    orch._incident_loop = SimpleNamespace(_code_index=SimpleNamespace(refresh=refresh))

    async def stop_soon():
        await asyncio.sleep(0.05)
        orch._running = False
    async def nothing():
        await asyncio.sleep(10)
    monkeypatch.setattr("app.services.orchestrator.event_queue.dequeue", nothing)
    async def empty_queue(coro, timeout):                         # what an idle queue does: time out
        coro.close()
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError
    monkeypatch.setattr("app.services.orchestrator.asyncio.wait_for", empty_queue)
    await asyncio.gather(orch.run_forever(), stop_soon())
    assert started == [True]
