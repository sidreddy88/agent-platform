"""
Weekly agent monitoring with agentstats: the TriageAgent, end to end.

    python scripts/agentstats_weekly.py --days 7 --out weekly/          # report.md + artifacts
    python scripts/agentstats_weekly.py --days 7 --out weekly/ --judge  # also the rubric judge (Together key)

Steps, all on last week's Langfuse traces:
1. Fetch every TriageAgent run (agent observation: the event in, the decision out) and every tool call, through the
   Langfuse v2 observations API, and join them by trace id. Tool rows don't carry the trace name, so tools are
   fetched for the whole window and matched to the triage traces.
2. Build one agentstats row per run, tagged by source: `production` (a real log group), `eval` (a synthetic one,
   i.e. a gate or eval replay), `no_log_group`. Rows are rules-only: production has no labels. Behaviour checks:
   - called_check_duplicate_pr   every run
   - called_get_occurrence_count when the event has a log group
   - count_matches_lookup        when the count tool ran and the answer states occurrences_24h: the stated count is
                                 one the tool returned, or 1 when every lookup found none (made-up counts were a
                                 real production bug)
   An answer that isn't the JSON the agent should return is no_answer (parse_error).
3. Summarize the rules per source with agentstats (Wilson intervals; production is a sample of traffic).
4. With --judge: `agentstats grade` on a sample of runs with config/agentstats/triage_rubric.json (severity follows
   the guide, decision supported, reasoning sticks to the facts), then `agentstats review sample` for a person.
5. `agentstats promote find`: failed runs become candidate eval cases (deduplicated by signature, with provenance).
6. report.md: aggregates, trace ids and error types only (no raw event text, which can contain production data).

The severity guide the judge sees is read from the triage prompt (app/agents/triage.py), so there's one copy.
Needs only the standard library and agentstats. Langfuse keys from LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
(LANGFUSE_BASE_URL optional).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUBRIC = ROOT / "config" / "agentstats" / "triage_rubric.json"
TRIAGE_PROMPT = ROOT / "app" / "agents" / "triage.py"
RULE_FLOOR = 0.95                       # a behaviour rule holding less often than this (proven) is flagged
MAX_JUDGE_FLIPS = 0.10                  # above this the judge is too unstable to report its verdicts as findings
COUNT_RE = re.compile(r"^\s*(\d+)\s+occurrences?\b", re.I)


# ---- 1. fetch --------------------------------------------------------------------------------------------------

def _langfuse_get(params: dict, retries: int = 6) -> dict:
    """One page from the Langfuse v2 observations API. Rate limits (429) and server errors are retried with backoff,
    honouring Retry-After: paging through a week of generations hits the limit."""
    base = (os.environ.get("LANGFUSE_BASE_URL") or "https://us.cloud.langfuse.com").rstrip("/")
    token = base64.b64encode(f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode())
    url = f"{base}/api/public/v2/observations?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + token.decode(),
                                               "User-Agent": "agentstats-weekly"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or attempt == retries:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else min(2 ** attempt * 2, 60))
    raise RuntimeError("unreachable")


def fetch_observations(obs_type: str, start: dt.datetime, end: dt.datetime, name: str | None = None,
                       get=_langfuse_get, max_rows: int = 50_000,
                       fields: str = "core,basic,io,metadata") -> list[dict]:
    flt = [{"type": "datetime", "column": "startTime", "operator": ">=", "value": start.isoformat()},
           {"type": "datetime", "column": "startTime", "operator": "<", "value": end.isoformat()}]
    if name:
        flt.append({"type": "string", "column": "name", "operator": "=", "value": name})
    params = {"type": obs_type, "limit": 1000, "fields": fields, "filter": json.dumps(flt)}
    rows: list[dict] = []
    while len(rows) < max_rows:
        page = get(params)
        rows += page.get("data", [])
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor:
            break
        params = {**params, "cursor": cursor}
    return rows


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def join_traces(agents: list[dict], tools: list[dict], generations: list[dict] | None = None) -> list[dict]:
    """One dict per TriageAgent run: the agent observation, its tool calls in start order, and from its LLM calls
    the model and the summed cost."""
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for t in tools:
        by_trace[t["traceId"]].append(t)
    models: dict[str, Counter] = defaultdict(Counter)
    cost: dict[str, float] = defaultdict(float)
    for g in generations or []:
        if g.get("model"):
            models[g["traceId"]][g["model"]] += 1
        cost[g["traceId"]] += float(g.get("totalCost") or 0)
    out = []
    for a in agents:
        calls = sorted(by_trace.get(a["traceId"], []), key=lambda t: t.get("startTime") or "")
        m = models.get(a["traceId"])
        out.append({"trace_id": a["traceId"], "agent": a, "model": m.most_common(1)[0][0] if m else "unknown",
                    "cost_usd": cost.get(a["traceId"]),
                    "calls": [{"name": t.get("name"), "input": _json(t.get("input")) or {},
                               "output": str(_json(t.get("output")) or "")} for t in calls]})
    return out


# ---- 2. rows ---------------------------------------------------------------------------------------------------

def event_text(agent_obs: dict) -> str:
    inp = _json(agent_obs.get("input"))
    if isinstance(inp, dict):
        inp = inp.get("user_input", "")
    return str(inp or "")


def event_field(text: str, field: str) -> str | None:
    m = re.search(rf"^\s*{field}\s*:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def log_group(trace: dict) -> str | None:
    for c in trace["calls"]:
        if c["name"] == "get_occurrence_count" and isinstance(c["input"], dict) and c["input"].get("log_group"):
            return str(c["input"]["log_group"])
    lg = event_field(event_text(trace["agent"]), "log_group")
    return lg if lg and lg.lower() not in ("none", "null", "") else None


def source(trace: dict) -> str:
    """production / local / eval / no_log_group. Traces carry a Langfuse environment since agent-platform set one
    (production on ECS, local elsewhere); older traces only say "default", so the log group decides: a synthetic
    one is an eval run, a real one is production (which then includes local replays of real incidents)."""
    env = (trace["agent"].get("environment") or "default").lower()
    if env in ("production", "local"):
        lg = log_group(trace)
        return "eval" if lg and "synthetic" in lg.lower() else env
    lg = log_group(trace)
    if not lg:
        return "no_log_group"
    return "eval" if "synthetic" in lg.lower() else "production"


def decision(agent_obs: dict) -> dict | None:
    """The triage JSON the agent answered with, or None if it isn't parseable."""
    out = _json(agent_obs.get("output"))
    if isinstance(out, dict) and "answer" in out:
        out = out["answer"]
    if isinstance(out, str):
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return None
        try:
            out = json.loads(m.group(0))
        except ValueError:
            return None
    return out if isinstance(out, dict) and "decision" in out else None


def looked_up_counts(trace: dict) -> list[int]:
    """Every count the occurrence tool returned in this run (the agent may look up more than one pattern)."""
    out = []
    for c in trace["calls"]:
        if c["name"] == "get_occurrence_count":
            m = COUNT_RE.match(c["output"])
            if m:
                out.append(int(m.group(1)))
    return out


def count_supported(stated: int, looked_up: list[int]) -> bool:
    """The stated occurrences_24h is one the tool returned, or 1 when every lookup found none (the event being
    triaged is itself one occurrence; the agent may reasonably treat the log count as a lower bound)."""
    return stated in looked_up or (stated == 1 and all(n == 0 for n in looked_up))


def to_row(trace: dict) -> dict:
    agent = trace["agent"]
    if agent.get("level") == "ERROR":           # the run itself failed (e.g. the LLM circuit breaker was open)
        return {"arm": "triage", "case": trace["trace_id"], "run": 1, "passed": None,
                "infra_error": str(agent.get("statusMessage") or "agent run errored")[:200],
                "tags": [f"source:{source(trace)}", f"model:{trace.get('model', 'unknown')}"], "checks": {}}
    names = [c["name"] for c in trace["calls"]]
    d = decision(trace["agent"])
    checks = {"called_check_duplicate_pr": "check_duplicate_pr" in names}
    if log_group(trace):
        checks["called_get_occurrence_count"] = "get_occurrence_count" in names
    looked_up = looked_up_counts(trace)
    stated = (d or {}).get("occurrences_24h")
    if looked_up and isinstance(stated, (int, float)) and not isinstance(stated, bool):
        checks["count_matches_lookup"] = count_supported(int(stated), looked_up)
    error_type = event_field(event_text(trace["agent"]), "error_type") or "unknown"
    meta = trace["agent"].get("metadata") or {}
    row = {"arm": "triage", "case": trace["trace_id"], "run": 1, "passed": None, "infra_error": None,
           "tags": [f"source:{source(trace)}", f"model:{trace.get('model', 'unknown')}", f"error_type:{error_type}",
                    f"severity:{(d or {}).get('severity', 'none')}", f"decision:{(d or {}).get('decision', 'none')}"],
           "checks": checks, "tool_calls": len(names)}
    if trace.get("cost_usd"):
        row["cost_usd"] = round(trace["cost_usd"], 6)
    if trace["agent"].get("latency") is not None:
        row["latency_s"] = float(trace["agent"]["latency"])
    if isinstance(meta, dict) and meta.get("iterations") is not None:
        row["steps"] = float(meta["iterations"])
    if d is None:
        row["no_answer"] = "parse_error"
    return row


# ---- 4. judge items --------------------------------------------------------------------------------------------

def severity_guide() -> str:
    lines = [ln.strip() for ln in TRIAGE_PROMPT.read_text().splitlines() if re.match(r"^\s*P[0-3]\s+—", ln)]
    return "\n".join(lines)


def to_item(trace: dict, guide: str) -> dict | None:
    d = decision(trace["agent"])
    if d is None:
        return None
    text = event_text(trace["agent"])
    event = "\n".join(f"{f}: {(event_field(text, f) or '')[:600]}" for f in ("error_type", "title", "service",
                                                                               "description"))
    tools = "\n".join(f"{c['name']}: {c['output'][:300]}" for c in trace["calls"]) or "no tool calls"
    agent = json.dumps({k: d.get(k) for k in ("decision", "severity", "occurrences_24h", "reasoning")})
    return {"item": trace["trace_id"], "case": trace["trace_id"], "tags": [f"source:{source(trace)}"],
            "severity_guide": guide, "error_event": event, "tool_results": tools, "agent_decision": agent}


# ---- 3/6. report -----------------------------------------------------------------------------------------------

def _pct(v):
    return "n/a" if v is None else f"{100 * v:.1f}%"


def summarize_rules(rows: list[dict]) -> dict[str, dict]:
    from agentstats import analysis
    from agentstats.records import from_dict
    def tag(r, prefix):
        return next((t.split(":", 1)[1] for t in r["tags"] if t.startswith(prefix + ":")), "unknown")
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[f"{tag(r, 'source')} / {tag(r, 'model')}"].append(r)
    out = {}
    for key in sorted(groups, key=lambda k: (not k.startswith("production"), k)):
        atts = [from_dict(r) for r in groups[key]]
        res = analysis.summarize(atts, design="sampled")
        costs = [r["cost_usd"] for r in groups[key] if r.get("cost_usd")]
        out[key] = {"runs": len(atts), "no_answer": sum(1 for a in atts if a.no_answer),
                    "infra": sum(1 for a in atts if a.infra_error),
                    "cost_per_run": sum(costs) / len(costs) if costs else None, "checks": res.get("checks", {})}
    return out


def flags(rule_summary: dict[str, dict]) -> list[str]:
    """A rule whose whole interval is below the floor is proven to hold too rarely; one whose estimate is below it is
    a lead to watch."""
    out = []
    for src, s in rule_summary.items():
        for rule, iv in s["checks"].items():
            if iv["high"] < RULE_FLOOR:
                out.append(f"**{src}: `{rule}` holds only {_pct(iv['estimate'])} (CI {_pct(iv['low'])} to "
                           f"{_pct(iv['high'])}), proven below {_pct(RULE_FLOOR)}**")
            elif iv["estimate"] < RULE_FLOOR:
                out.append(f"{src}: `{rule}` at {_pct(iv['estimate'])} (CI {_pct(iv['low'])} to {_pct(iv['high'])}):"
                           " below the floor but not proven; watch it")
    return out


def render(start, end, rows, rule_summary, judge_summary, review_note, promote_table, failures) -> str:
    by_src = Counter(t.split(":", 1)[1] for r in rows for t in r["tags"] if t.startswith("source:"))
    lines = [f"# agentstats weekly: TriageAgent, {start:%Y-%m-%d} to {end:%Y-%m-%d}", "",
             f"{len(rows)} runs: " + ", ".join(f"{k} {v}" for k, v in sorted(by_src.items())) + ".", ""]
    fl = flags(rule_summary)
    lines += ["## Needs attention", ""] + ([f"- {f}" for f in fl] or ["- Nothing: every behaviour rule is at or "
                                                                     f"above {_pct(RULE_FLOOR)}."]) + [""]
    lines += ["## Runs by source and model", "",
              "| source / model | runs | errored (infrastructure) | no usable answer | cost per run |",
              "|---|---|---|---|---|"]
    for key, s in rule_summary.items():
        cpr = f"${s['cost_per_run']:.4f}" if s.get("cost_per_run") else "n/a"
        lines.append(f"| {key} | {s['runs']} | {s['infra']} | {s['no_answer']} | {cpr} |")
    errors = Counter(r["infra_error"] for r in rows if r.get("infra_error"))
    if errors:
        lines += ["", "Errored runs are kept out of the rule rates. Causes: "
                  + "; ".join(f"{msg} ({n})" for msg, n in errors.most_common(3)) + "."]
    lines += ["", "## Behaviour rules (no labels needed)", "",
              "| source / model | rule | runs checked | held | 95% CI |", "|---|---|---|---|---|"]
    for src, s in rule_summary.items():
        for rule, iv in sorted(s["checks"].items()):
            lines.append(f"| {src} | `{rule}` | {iv['n']} | {_pct(iv['estimate'])} | {_pct(iv['low'])} to "
                         f"{_pct(iv['high'])} |")
    lines.append("")
    if failures:
        lines += ["Runs that broke a rule (trace ids, for Langfuse): " + ", ".join(f"`{t}`" for t in failures[:15])
                  + (f" and {len(failures) - 15} more" if len(failures) > 15 else ""), ""]
    lines += ["## Rubric judge", "", judge_summary or "Not run this week (run with `--judge`).", ""]
    lines += ["## Human review", "", review_note, ""]
    lines += ["## Candidate eval cases (failures, deduplicated)", "", promote_table or "None this week.", ""]
    lines += ["## How to close the loop", "",
              "1. Download the `agentstats-weekly` artifact from this run.",
              "2. Fill `review/review.csv` (human_verdict: pass / fail / unsure), then "
              "`agentstats review import review/review.csv --key review/key.jsonl --rater <you> "
              "--out app/evals/agentstats/triage_review_labels.jsonl` and commit the labels.",
              "3. In `candidates.jsonl`, set `\"approved\": true` on cases worth keeping, then "
              "`agentstats promote accept candidates.jsonl --into app/evals/agentstats/triage_cases.jsonl`, and add "
              "their events (from `items.jsonl`) with your label to the triage eval set.", "",
              "_Generated by `scripts/agentstats_weekly.py` with agentstats. No raw event text is included here; it's "
              "in the artifact and Langfuse._"]
    return "\n".join(lines) + "\n"


# ---- main ------------------------------------------------------------------------------------------------------

def _agentstats(*args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "agentstats.cli", *args], capture_output=True, text=True)
    if r.returncode not in (0, 1, 2, 3):
        raise RuntimeError(f"agentstats {' '.join(args[:2])} failed: {r.stderr[-500:]}")
    return r.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--judge", action="store_true", help="run the rubric judge on a sample (TOGETHER_API_KEY)")
    ap.add_argument("--judge-sample", type=int, default=40, help="runs judged per week (production first)")
    ap.add_argument("--judge-model", default="deepseek-ai/DeepSeek-V4.1-Flash")
    ap.add_argument("--review-n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    end = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = end - dt.timedelta(days=args.days)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    agents = fetch_observations("AGENT", start, end, name="TriageAgent")
    tools = fetch_observations("TOOL", start, end)
    # LLM calls are named by whichever path made them ("llm/triage" through the gateway, "anthropic.messages.create"
    # from the SDK's own instrumentation, ...), so take every generation in the window and match by trace id.
    generations = fetch_observations("GENERATION", start, end, fields="core,model,usage", max_rows=200_000)
    traces = join_traces(agents, tools, generations)
    rows = [to_row(t) for t in traces]
    (out / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(traces)} TriageAgent runs, {len(tools)} tool calls in the window")
    if not rows:
        (out / "report.md").write_text(f"# agentstats weekly: TriageAgent\n\nNo TriageAgent runs between "
                                       f"{start:%Y-%m-%d} and {end:%Y-%m-%d}.\n")
        return 0

    rule_summary = summarize_rules(rows)
    failures = [r["case"] for r in rows if not all(r["checks"].values()) or r.get("no_answer")]

    judge_summary, review_note = "", "Not run this week (needs the judge: `--judge`)."
    guide = severity_guide()
    items = [i for i in (to_item(t, guide) for t in traces) if i]
    (out / "items.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
    if args.judge and items:
        rng = random.Random(args.seed)
        prod = [i for i in items if "source:production" in i["tags"]]
        rest = [i for i in items if "source:production" not in i["tags"]]
        rng.shuffle(rest)
        sample = (prod + rest)[:args.judge_sample]
        (out / "judge_items.jsonl").write_text("".join(json.dumps(i) + "\n" for i in sample))
        _agentstats("grade", str(out / "judge_items.jsonl"), "--rubric", str(RUBRIC), "--model", args.judge_model,
                    "--base-url", "https://api.together.xyz/v1", "--api-key-env", "TOGETHER_API_KEY",
                    "--fields", "severity_guide,error_event,tool_results,agent_decision", "--repeats", "2",
                    "--out", str(out / "judge_labels.jsonl"), "--rows", str(out / "judge_rows.jsonl"))
        jrows = [json.loads(x) for x in (out / "judge_rows.jsonl").read_text().splitlines() if x.strip()]
        judged = [r for r in jrows if r["passed"] is not None]
        per_q = Counter(k for r in judged for k, v in r["checks"].items() if not v)
        consistency = _agentstats("judge", str(out / "judge_labels.jsonl"))
        flip = re.search(r"flip rate ([\d.]+)%", consistency)
        flip_rate = float(flip.group(1)) / 100 if flip else None
        judge_summary = (f"{len(judged)} of {len(jrows)} sampled runs judged; {sum(r['passed'] for r in judged)} "
                         f"passed every rubric question. Failed questions: "
                         + (", ".join(f"`{k}` {v}" for k, v in per_q.most_common()) or "none")
                         + f". Judge flip rate over 2 repeats: {_pct(flip_rate)}.")
        if flip_rate is not None and flip_rate > MAX_JUDGE_FLIPS:
            judge_summary = (f"**The judge is unreliable this week** (flip rate {_pct(flip_rate)} over 2 repeats, above "
                             f"{_pct(MAX_JUDGE_FLIPS)}): treat its verdicts as noise and fix the rubric before "
                             "trusting them. " + judge_summary)
        review_dir = out / "review"
        _agentstats("review", "sample", str(out / "judge_rows.jsonl"), "--n", str(args.review_n),
                    "--stratify-by", "source", "--seed", str(args.seed), "--out", str(review_dir))
        review_note = (f"{args.review_n} judged runs sampled for review (blind sheet `review/review.csv` in the "
                       "artifact; verdicts and sampling weights in `review/key.jsonl`).")

    # Only production failures become candidates: eval failures are already eval cases.
    prod_rows = [r for r in rows if "source:production" in r["tags"]]
    (out / "production_rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in prod_rows))
    promote_out = _agentstats("promote", "find", str(out / "production_rows.jsonl"), "--source", "production",
                              "--label-from", "human", "--group-by", "error_type",
                              "--out", str(out / "candidates.jsonl")) if prod_rows else ""
    table = "\n".join(ln for ln in promote_out.splitlines() if ln.startswith("|"))
    report = render(start, end, rows, rule_summary, judge_summary, review_note, table, failures)
    (out / "report.md").write_text(report)
    (out / "rules.json").write_text(json.dumps(rule_summary, indent=1))
    print(f"wrote {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
