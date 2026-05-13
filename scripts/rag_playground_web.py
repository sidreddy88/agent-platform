"""
Browser-based RAG playground.

Run from repo root:

    pip install streamlit
    streamlit run scripts/rag_playground_web.py

Streamlit opens a tab at http://localhost:8501. Six tabs cover the same
ground as the CLI playground but with charts, side-by-side comparisons,
and clickable corpus browsing.

Designed for LEARNING — not a polished product. Every page has a "what
this teaches you" note so concepts ladder up.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st  # noqa: E402

from app.services.rag import RAGService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def _verdict(score: float) -> str:
    if score >= 0.95:
        return "near-identical"
    if score >= 0.85:
        return "same idea, slight rewording"
    if score >= 0.60:
        return "same topic"
    if score >= 0.30:
        return "loosely related"
    if score >= 0.10:
        return "barely related"
    return "unrelated"


def _verdict_color(score: float) -> str:
    if score >= 0.85:
        return "green"
    if score >= 0.60:
        return "blue"
    if score >= 0.30:
        return "orange"
    return "red"


@st.cache_resource
def get_rag() -> RAGService:
    """Cached so we don't reconnect to the corpus on every Streamlit rerun."""
    return RAGService()


def run_async(coro):
    """Streamlit is sync. Wrap an async call so we can use it from tabs."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

st.set_page_config(page_title="RAG Playground", layout="wide", page_icon="🔎")
st.title("🔎 RAG Playground")
st.caption(
    "Local exploration of the incident + codebase RAG corpora. "
    "Each tab teaches one concept by letting you do it."
)

rag = get_rag()
incident_n = rag._incident_collection.count()
codebase_n = rag._collection.count()

# Sticky header summary
col1, col2 = st.columns(2)
col1.metric("Incident corpus", f"{incident_n} docs")
col2.metric("Codebase corpus", f"{codebase_n} chunks")

st.markdown("---")

tab_query, tab_compare, tab_embed, tab_dist, tab_pair, tab_browse, tab_stats = st.tabs(
    [
        "🔍 Query",
        "⚖️ Compare two strings",
        "🧬 Embedding inspector",
        "📊 Score distribution",
        "🔁 Query sensitivity (pair)",
        "📚 Browse corpus",
        "📈 Stats",
    ]
)


# ---------------------------------------------------------------------------
# Tab: Query
# ---------------------------------------------------------------------------

with tab_query:
    st.subheader("Search the incident corpus")
    st.caption(
        "Type a query. The app embeds it, finds the top-K closest documents "
        "in the corpus by cosine similarity, and shows them with scores."
    )

    q = st.text_input("Query", value="jwt token expired", key="q_input")
    cols = st.columns(2)
    k = cols[0].slider("Top K", 1, 30, 10, key="q_k")
    threshold = cols[1].slider("Threshold (visual marker only)", 0.0, 1.0, 0.80, 0.05, key="q_thresh")

    if q:
        if incident_n == 0:
            st.warning("Incident corpus is empty. Index some incidents first.")
        else:
            try:
                results = run_async(rag.search_incidents(q, n_results=k, min_score=0.0))
            except Exception as exc:
                st.error(f"Search failed: {exc}")
                results = []

            if not results:
                st.warning("No results — likely an embedding call failed. Check OPENAI_API_KEY.")
            else:
                top = results[0]["score"]
                st.markdown(
                    f"**Top score: `{top:.3f}`** — :{_verdict_color(top)}[{_verdict(top)}]"
                )

                for i, r in enumerate(results, 1):
                    label = "✓" if r["score"] >= threshold else "·"
                    with st.expander(
                        f"{label} #{i}  score={r['score']:.3f}  "
                        f"[{r['error_type'] or '?'}]  {r['text'][:90]}",
                        expanded=False,
                    ):
                        c1, c2 = st.columns([3, 1])
                        c1.code(r["text"], language="text")
                        c2.markdown(
                            f"**incident_id:** `{r['incident_id'][:8]}`\n\n"
                            f"**status:** {r['status']}\n\n"
                            f"**service:** {r['service']}\n\n"
                            f"**pr:** {r['pr_url'] or '—'}\n\n"
                            f"**score:** {r['score']:.4f}"
                        )

    with st.expander("ℹ️ What this teaches", expanded=False):
        st.markdown(
            """
            **The whole RAG retrieval algorithm in three steps:**

            1. Your query string → 1536-d vector (via OpenAI's embedding API)
            2. Compare the query vector to every doc vector in the corpus using cosine similarity
            3. Sort high to low, return top-K

            That's it. The "intelligence" lives in step 1 — the embedding model was
            trained so that similar meanings produce similar vectors. Step 2 is
            pure math.

            **What to try:**
            - A query you'd expect to hit (something from `list`) → top score should be > 0.7
            - A query that's unrelated to your corpus → top scores will all be < 0.4
            - Same query rephrased — does it return the same top-K?
            """
        )


# ---------------------------------------------------------------------------
# Tab: Compare
# ---------------------------------------------------------------------------

with tab_compare:
    st.subheader("Cosine similarity between two arbitrary strings")
    st.caption(
        "No corpus involved — just embed both strings and compute cosine. "
        "Best tool for building intuition about what similarity scores feel like."
    )

    c1, c2 = st.columns(2)
    a = c1.text_area("Text A", value="jwt token expired", height=80, key="cmp_a")
    b = c2.text_area("Text B", value="TokenExpiredError: jwt expired", height=80, key="cmp_b")

    if a and b:
        try:
            embs = run_async(rag._embed([a, b]))
        except Exception as exc:
            st.error(f"Embedding failed: {exc}")
            embs = None

        if embs:
            score = _cosine(embs[0], embs[1])
            color = _verdict_color(score)
            verdict = _verdict(score)

            st.markdown("### Result")
            st.metric("Cosine similarity", f"{score:.4f}", help=verdict)
            st.markdown(f"**Verdict:** :{color}[{verdict}]")
            st.progress(min(max(score, 0.0), 1.0))

            # Side-by-side embedding visualisation (first 32 dims)
            st.markdown("### What the vectors look like (first 32 of 1536 dims)")
            st.caption(
                "Two embeddings shown side by side. Notice how similar-meaning "
                "text produces similar bar patterns. Compare to an unrelated string."
            )

            import pandas as pd  # noqa: E402

            top_n = 32
            df = pd.DataFrame({
                "dim": list(range(top_n)),
                "A": embs[0][:top_n],
                "B": embs[1][:top_n],
            })
            st.line_chart(df.set_index("dim"), height=200)

            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("dim", "1536")
            cc2.metric("‖A‖₂", f"{math.sqrt(sum(x*x for x in embs[0])):.4f}")
            cc3.metric("‖B‖₂", f"{math.sqrt(sum(x*x for x in embs[1])):.4f}")

    with st.expander("ℹ️ What this teaches", expanded=False):
        st.markdown(
            """
            **Build intuition for the score scale by trying pairs you have an opinion on:**

            | Try comparing | Expected |
            |---|---|
            | "jwt expired" vs "jwt token expired" | ~0.90 |
            | "jwt expired" vs "TokenExpiredError" | ~0.75–0.85 |
            | "jwt expired" vs "image processing failed" | ~0.30 |
            | "jwt expired" vs "I went to the grocery store" | ~0.10 |
            | "X" vs "X" (same on both sides) | exactly 1.0 |

            The chart at the bottom shows the first 32 of 1536 vector dimensions
            for each input. Similar meanings → similar bar patterns. This is the
            "language model arranges similar text to land at similar coordinates"
            claim, made concrete.
            """
        )


# ---------------------------------------------------------------------------
# Tab: Embed inspector
# ---------------------------------------------------------------------------

with tab_embed:
    st.subheader("What does a single embedding actually look like?")
    st.caption(
        "The vector is just 1536 floats. Individual values mean nothing on their own — "
        "the information is in the pattern."
    )

    text = st.text_input("Text to embed", value="jwt token expired", key="embed_input")

    if text:
        try:
            embs = run_async(rag._embed([text]))
            vec = embs[0]
        except Exception as exc:
            st.error(f"Embedding failed: {exc}")
            vec = None

        if vec:
            norm = math.sqrt(sum(x * x for x in vec))
            cc1, cc2, cc3, cc4 = st.columns(4)
            cc1.metric("dim", len(vec))
            cc2.metric("L2 norm", f"{norm:.4f}", help="~1.0 because the model returns L2-normalised vectors")
            cc3.metric("min value", f"{min(vec):+.4f}")
            cc4.metric("max value", f"{max(vec):+.4f}")

            import pandas as pd  # noqa: E402

            top_n = st.slider("Show first N dimensions", 8, 256, 64, key="embed_slider")
            df = pd.DataFrame({"dim": list(range(top_n)), "value": vec[:top_n]})
            st.bar_chart(df.set_index("dim"), height=240)

            with st.expander("Show raw first-32 dimensions"):
                st.code(", ".join(f"{v:+.6f}" for v in vec[:32]))

    with st.expander("ℹ️ What this teaches", expanded=False):
        st.markdown(
            """
            **The vector is just a list of floats.** No individual number is
            meaningful on its own. The chart shows you the *pattern* — and the
            point is that similar-meaning text produces similar patterns.

            Re-run the same text twice — you'll get the same vector each time
            (the embedding model is deterministic).

            Now try the same text with different framings, e.g. "jwt expired"
            vs "JWT token expired" vs "the jwt has expired" — patterns will be
            similar but not identical.
            """
        )


# ---------------------------------------------------------------------------
# Tab: Distribution
# ---------------------------------------------------------------------------

with tab_dist:
    st.subheader("Score distribution across the entire corpus")
    st.caption(
        "For a given query, what does the FULL distribution of scores look like? "
        "Top-K hides this. Looking at the whole shape tells you 'is this a hit, "
        "a near-miss, or a cold start?'"
    )

    q = st.text_input("Query for distribution", value="jwt token expired", key="dist_input")

    if q and incident_n > 0:
        try:
            results = run_async(rag.search_incidents(q, n_results=incident_n, min_score=0.0))
        except Exception as exc:
            st.error(f"Search failed: {exc}")
            results = []

        if results:
            scores = [r["score"] for r in results]
            top = max(scores)
            median = sorted(scores)[len(scores) // 2]
            mean = sum(scores) / len(scores)

            c1, c2, c3 = st.columns(3)
            c1.metric("Top", f"{top:.3f}")
            c2.metric("Median", f"{median:.3f}")
            c3.metric("Mean", f"{mean:.3f}")

            import pandas as pd  # noqa: E402

            # 20 bins from 0.0 to 1.0
            bins = [0] * 20
            for s in scores:
                idx = min(int(max(s, 0.0) * 20), 19)
                bins[idx] += 1
            df = pd.DataFrame({
                "score_bin": [f"{i*0.05:.2f}" for i in range(20)],
                "count": bins,
            })
            st.bar_chart(df.set_index("score_bin"), height=300)

            # Verdict
            if top >= 0.80:
                st.success(f"**Hit** — top score {top:.3f} indicates the corpus has relevant docs.")
            elif top >= 0.60:
                st.warning(f"**Near-miss** — top score {top:.3f}. Related docs exist but threshold may not catch them.")
            else:
                st.error(f"**Cold start** — top score {top:.3f}. The corpus doesn't contain relevant docs for this query.")

    with st.expander("ℹ️ What this teaches", expanded=False):
        st.markdown(
            """
            **The shape of the distribution tells you what kind of corpus response you got:**

            - **Spike at high end + long tail below** → real hit, one or two docs are very relevant.
            - **Cluster around 0.5–0.7** → near-miss, related docs exist but nothing strongly relevant.
            - **All scores clustered around 0.2** → cold start, the corpus has nothing on this topic.

            Top-K alone hides this — you always get K results, even if all of them
            are bad. The distribution gives you the honest picture.

            **What to try:**
            - A query about an error_type that's in your corpus → expect a spike
            - A query about something not in your corpus → expect a low flat distribution
            """
        )


# ---------------------------------------------------------------------------
# Tab: Pair (query sensitivity)
# ---------------------------------------------------------------------------

with tab_pair:
    st.subheader("Query sensitivity — how stable is retrieval under rewording?")
    st.caption(
        "Run two related queries side-by-side. The Jaccard overlap of their top-10 "
        "result sets tells you how robust your retrieval is to phrasing changes."
    )

    pc1, pc2 = st.columns(2)
    qa = pc1.text_input("Query A", value="jwt token expired", key="pair_a")
    qb = pc2.text_input("Query B", value="auth token has expired", key="pair_b")

    if qa and qb and incident_n > 0:
        try:
            ra = run_async(rag.search_incidents(qa, n_results=10, min_score=0.0))
            rb = run_async(rag.search_incidents(qb, n_results=10, min_score=0.0))
        except Exception as exc:
            st.error(f"Search failed: {exc}")
            ra, rb = [], []

        if ra and rb:
            ids_a = {r["incident_id"] for r in ra}
            ids_b = {r["incident_id"] for r in rb}
            shared = ids_a & ids_b
            union = ids_a | ids_b
            jaccard = len(shared) / len(union) if union else 0.0

            c1, c2, c3 = st.columns(3)
            c1.metric("Jaccard overlap", f"{jaccard:.2f}")
            c2.metric("Shared", f"{len(shared)} / {len(union)}")
            top_a = ra[0]["score"] if ra else 0
            top_b = rb[0]["score"] if rb else 0
            c3.metric("Δ top score", f"{abs(top_a - top_b):.3f}")

            pc1, pc2 = st.columns(2)
            with pc1:
                st.markdown(f"**Top 5 for A:** *{qa}*")
                for r in ra[:5]:
                    marker = "🟢" if r["incident_id"] in shared else "⚪"
                    st.markdown(f"{marker} `{r['score']:.3f}` {r['text'][:80]}")
            with pc2:
                st.markdown(f"**Top 5 for B:** *{qb}*")
                for r in rb[:5]:
                    marker = "🟢" if r["incident_id"] in shared else "⚪"
                    st.markdown(f"{marker} `{r['score']:.3f}` {r['text'][:80]}")

    with st.expander("ℹ️ What this teaches", expanded=False):
        st.markdown(
            """
            **Two queries that mean the same thing SHOULD return similar top-Ks.**
            If they don't, your retrieval is brittle — small wording changes drift
            results, which means production users will get inconsistent answers
            depending on how they phrased their question.

            Aim for Jaccard ≥ 0.6 for well-rephrased synonyms. If you're seeing
            < 0.4, your indexed text or your embedding model is too sensitive
            to surface wording.

            **What to try:**
            - "jwt expired" vs "auth token has expired" → should overlap a lot
            - "S3 upload failed" vs "image upload error" → should overlap less
              (different concepts even though both are errors)
            - Try a question form vs a declarative form: "why is the upload
              failing?" vs "upload failure" → are they retrieving similarly?
            """
        )


# ---------------------------------------------------------------------------
# Tab: Browse corpus
# ---------------------------------------------------------------------------

with tab_browse:
    st.subheader("Browse the incident corpus")
    st.caption("Every doc in the corpus. Click to see its full indexed text and metadata.")

    if incident_n == 0:
        st.info("Corpus is empty.")
    else:
        items = list(rag._incident_collection.all_items())

        # Optional filter
        error_types = sorted({it.metadata.get("error_type", "?") for it in items})
        selected_types = st.multiselect(
            "Filter by error_type",
            options=error_types,
            default=error_types,
        )
        filtered = [it for it in items if it.metadata.get("error_type", "?") in selected_types]
        st.caption(f"Showing {len(filtered)} of {len(items)} docs")

        for it in filtered:
            meta = it.metadata
            title = f"`{meta.get('incident_id', '?')[:8]}` — {meta.get('error_type','?')} — {meta.get('service','?')}"
            with st.expander(title, expanded=False):
                c1, c2 = st.columns([3, 1])
                c1.code(it.document, language="text")
                c2.markdown(
                    f"**id:** `{meta.get('incident_id', '?')}`\n\n"
                    f"**status:** {meta.get('status', '?')}\n\n"
                    f"**service:** {meta.get('service', '?')}\n\n"
                    f"**error_type:** {meta.get('error_type', '?')}\n\n"
                    f"**pr:** {meta.get('pr_url') or '—'}\n\n"
                    f"**text length:** {len(it.document)} chars"
                )

    with st.expander("ℹ️ What this teaches", expanded=False):
        st.markdown(
            """
            **Read what's actually in your corpus.** RAG quality is mostly
            about what you embedded — not which retrieval algorithm you use.

            Things to notice:
            - Does each doc start with the same boilerplate? (signal-dilution)
            - Are some docs much longer than others? (length bias)
            - Are there duplicates that should have been deduped?
            - Is the content what a downstream agent needs, or does it contain
              ephemeral data (timestamps, IDs) that adds noise?
            """
        )


# ---------------------------------------------------------------------------
# Tab: Stats
# ---------------------------------------------------------------------------

with tab_stats:
    st.subheader("Corpus statistics")

    st.markdown("### Incident corpus")
    if incident_n == 0:
        st.info("Empty.")
    else:
        items = list(rag._incident_collection.all_items())
        lengths = sorted(len(it.document) for it in items)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("count", incident_n)
        c2.metric("min length", lengths[0])
        c3.metric("median length", lengths[len(lengths) // 2])
        c4.metric("max length", lengths[-1])

        import pandas as pd  # noqa: E402

        # Length histogram
        st.markdown("**Document length distribution**")
        df_lengths = pd.DataFrame({"length": lengths})
        st.bar_chart(df_lengths["length"].value_counts().sort_index(), height=200)

        # error_type breakdown
        by_type: dict[str, int] = {}
        for it in items:
            et = it.metadata.get("error_type", "?")
            by_type[et] = by_type.get(et, 0) + 1
        df_types = pd.DataFrame(
            sorted(by_type.items(), key=lambda x: -x[1]),
            columns=["error_type", "count"],
        )
        st.markdown("**Error type distribution**")
        st.bar_chart(df_types.set_index("error_type"), height=240)

    st.markdown("### Codebase corpus")
    if codebase_n == 0:
        st.info("Empty. Run `index <path>` in the CLI playground or hit POST /debug/rag/index.")
    else:
        try:
            metas = list(rag._collection.all_metadata())
            by_file: dict[str, int] = {}
            for m in metas:
                fp = m.get("file_path", "?")
                by_file[fp] = by_file.get(fp, 0) + 1
            c1, c2 = st.columns(2)
            c1.metric("total chunks", codebase_n)
            c2.metric("files indexed", len(by_file))

            import pandas as pd  # noqa: E402

            df = pd.DataFrame(
                sorted(by_file.items(), key=lambda x: -x[1])[:20],
                columns=["file", "chunks"],
            )
            st.markdown("**Top 20 files by chunk count**")
            st.bar_chart(df.set_index("file"), height=320)
        except Exception as exc:
            st.error(f"Could not fetch codebase metadata: {exc}")

    with st.expander("ℹ️ What this teaches", expanded=False):
        st.markdown(
            """
            **Corpus shape predicts retrieval behaviour.**

            - **Length distribution skewed short** → most docs are stubs;
              short docs lose to long docs on vector similarity because
              embeddings of short text have less information.
            - **One error_type dominates** → vector retrieval will bias toward
              that class regardless of the query.
            - **Few files indexed in codebase** → code-grounding will fail for
              any function that lives outside those files.

            For your corpus:
            - 82% S3_NO_SUCH_KEY → vector retrieval will surface S3 docs even
              for non-S3 queries.
            - Many docs at the minimum length → likely templated content.
            """
        )
