"""
Weekly agent monitoring with agentstats: every configured agent, end to end.

    python scripts/agentstats_weekly.py --days 7 --out weekly/                  # all agents in config/agentstats/agents
    python scripts/agentstats_weekly.py --days 7 --out weekly/ --judge          # also the rubric judge (Together key)
    python scripts/agentstats_weekly.py --days 7 --out weekly/ --agents triage  # just some

Each agent is a config file, config/agentstats/agents/<name>.json:
  agent          the Langfuse trace name (the agent span's name)
  rules          behaviour rules, checked per run:
                   {"name", "called": tool} / {"name", "not_called": tool} / {"name", "before": [a, b]} /
                   {"name", "args": {"tool", "match"}}  (agentstats rule kinds, on the run's recorded steps)
                   {"name", "output": field, "in": [...]} / {"name", "output": field, "min": n}  (the agent's output)
                 optional "when": an adapter condition name, "called:<tool>", "output:<field>>=<n>" or
                 "input_json:<key>" (a truthy key in a JSON user_input); the rule is only checked when it holds
  adapter        name in scripts/agentstats_adapters.py for logic a config can't express (default "default")
  rubric, judge_fields, judge_sample, review_n   the rubric judge and the human review sample (optional)
  group_by       tag prefix for candidate eval cases (optional)

Steps, on last week's Langfuse traces: fetch each agent's runs (agent observations by name) and every tool call
and LLM call in the window (Langfuse v2 observations API, joined by trace id) -> one agentstats row per run, tagged
by source (production / local / eval) and model, errored runs as infrastructure failures -> behaviour rules per
source and model (agentstats summarize, Wilson intervals) -> optional rubric judge on a sample (agentstats grade,
flagged unreliable above a 10% flip rate) and a blind review sheet (agentstats review sample) -> production
failures as candidate eval cases (agentstats promote find) -> one report.md with a section per agent: aggregates,
trace ids and error types only, no raw event text.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentstats.checks import run_checks  # noqa: E402
from agentstats_adapters import ADAPTERS, as_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config" / "agentstats" / "agents"
RULE_FLOOR = 0.95                       # a behaviour rule holding less often than this (proven) is flagged
MAX_JUDGE_FLIPS = 0.10                  # above this the judge is too unstable to report its verdicts as findings
AGENTSTATS_KINDS = ("called", "not_called", "before", "args")


# ---- config ----------------------------------------------------------------------------------------------------

def load_configs(names: list[str] | None = None, config_dir: Path = CONFIG_DIR) -> list[dict]:
    out = []
    for path in sorted(config_dir.glob("*.json")):
        if names and path.stem not in names:
            continue
        cfg = json.loads(path.read_text())
        cfg.setdefault("key", path.stem)
        if "agent" not in cfg:
            raise ValueError(f"{path}: 'agent' (the trace name) is required")
        if cfg.get("adapter", "default") not in ADAPTERS:
            raise ValueError(f"{path}: unknown adapter {cfg['adapter']!r}; known: {sorted(ADAPTERS)}")
        for r in cfg.get("rules", []):
            kinds = [k for k in (*AGENTSTATS_KINDS, "output") if k in r]
            if "name" not in r or len(kinds) != 1:
                raise ValueError(f"{path}: each rule needs a name and exactly one of "
                                 f"{(*AGENTSTATS_KINDS, 'output')}: {r}")
        out.append(cfg)
    return out


# ---- 1. fetch --------------------------------------------------------------------------------------------------

def _langfuse_get(params: dict, retries: int = 6) -> dict:
    """One page from the Langfuse v2 observations API. Rate limits (429) and server errors are retried with backoff,
    honouring Retry-After: paging through a week of generations hits the limit."""
    base = (os.environ.get("LANGFUSE_BASE_URL") or "https://us.cloud.langfuse.com").rstrip("/")
    # Stray quotes or whitespace (copied from a .env line like KEY="pk-lf-...") make Langfuse answer 401.
    public, secret = (os.environ[k].strip().strip("\"'") for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))
    token = base64.b64encode(f"{public}:{secret}".encode())
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


def join_traces(agents: list[dict], tools: list[dict], generations: list[dict] | None = None) -> list[dict]:
    """One dict per agent run: the agent observation, its tool calls in start order, and from its LLM calls the
    model and the summed cost. Tool and generation rows don't carry the trace name, so they're matched by id."""
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
                    "calls": [{"name": t.get("name"), "input": as_json(t.get("input")) or {},
                               "output": str(as_json(t.get("output")) or "")} for t in calls]})
    return out


# ---- 2. rows ---------------------------------------------------------------------------------------------------

def source(trace: dict, adapter) -> str:
    """production / local / eval / no_log_group / unknown. Traces carry a Langfuse environment (production on ECS,
    local elsewhere); older ones say "default", and for agents with a log group a synthetic one means an eval run
    and a real one production (which then includes local replays of real incidents)."""
    env = (trace["agent"].get("environment") or "default").lower()
    lg = adapter.log_group(trace)
    if lg and "synthetic" in lg.lower():
        return "eval"
    if env in ("production", "local"):
        return env
    if lg:
        return "production"
    return "no_log_group" if "log_group" in adapter.conditions else "unknown"


def condition_holds(when: str | None, trace: dict, output: dict | None, adapter) -> bool:
    if not when:
        return True
    names = [c["name"] for c in trace["calls"]]
    if when.startswith("called:"):
        return when.split(":", 1)[1] in names
    if when.startswith("output:"):
        m = re.match(r"output:(\w+)\s*>=\s*([\d.]+)$", when)
        if not m:
            raise ValueError(f"can't read condition {when!r}")
        v = (output or {}).get(m.group(1))
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= float(m.group(2))
    if when.startswith("input_json:"):
        inp = as_json(trace["agent"].get("input"))
        inp = as_json(inp.get("user_input")) if isinstance(inp, dict) and "user_input" in inp else inp
        return bool(isinstance(inp, dict) and inp.get(when.split(":", 1)[1]))
    fn = adapter.conditions.get(when)
    if fn is None:
        raise ValueError(f"unknown condition {when!r}")
    return bool(fn(trace))


def output_check(rule: dict, output: dict | None) -> bool:
    v = (output or {}).get(rule["output"])
    if "in" in rule:
        return v in rule["in"]
    if "min" in rule:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= rule["min"]
    return v is not None


def to_row(trace: dict, cfg: dict) -> dict:
    adapter = ADAPTERS[cfg.get("adapter", "default")]
    agent = trace["agent"]
    base_tags = [f"agent:{cfg['key']}", f"source:{source(trace, adapter)}", f"model:{trace.get('model', 'unknown')}"]
    if agent.get("level") == "ERROR":           # the run itself failed (e.g. the LLM circuit breaker was open)
        return {"arm": cfg["key"], "case": trace["trace_id"], "run": 1, "passed": None,
                "infra_error": str(agent.get("statusMessage") or "agent run errored")[:200],
                "tags": base_tags, "checks": {}}
    output = adapter.parse_output(trace)
    calls = [{"name": c["name"], "args": c["input"] if isinstance(c["input"], dict) else {}} for c in trace["calls"]]
    checks: dict[str, bool] = {}
    for r in cfg.get("rules", []):
        if not condition_holds(r.get("when"), trace, output, adapter):
            continue
        if "output" in r:
            if output is not None:
                checks[r["name"]] = output_check(r, output)
        else:
            checks.update(run_checks([{k: v for k, v in r.items() if k not in ("when", "why")}], calls))
    checks.update(adapter.checks(trace))
    meta = agent.get("metadata") or {}
    row = {"arm": cfg["key"], "case": trace["trace_id"], "run": 1, "passed": None, "infra_error": None,
           "tags": base_tags + adapter.tags(trace), "checks": checks, "tool_calls": len(calls)}
    if trace.get("cost_usd"):
        row["cost_usd"] = round(trace["cost_usd"], 6)
    if agent.get("latency") is not None:
        row["latency_s"] = float(agent["latency"])
    if isinstance(meta, dict) and meta.get("iterations") is not None:
        row["steps"] = float(meta["iterations"])
    if output is None:
        row["no_answer"] = adapter.no_answer_reason(trace)
    return row


# ---- 3. summaries ----------------------------------------------------------------------------------------------

def _pct(v):
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _tag(row: dict, prefix: str) -> str:
    return next((t.split(":", 1)[1] for t in row["tags"] if t.startswith(prefix + ":")), "unknown")


def summarize_rules(rows: list[dict]) -> dict[str, dict]:
    from agentstats import analysis
    from agentstats.records import from_dict
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[f"{_tag(r, 'source')} / {_tag(r, 'model')}"].append(r)
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


def _agentstats(*args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "agentstats.cli", *args], capture_output=True, text=True)
    if r.returncode not in (0, 1, 2, 3):
        raise RuntimeError(f"agentstats {' '.join(args[:2])} failed: {r.stderr[-500:]}")
    return r.stdout


def judge(cfg: dict, traces: list[dict], out: Path, seed: int) -> tuple[str, str]:
    """(judge summary, review note) for one agent: rubric judge on a sample, then a blind review sheet."""
    adapter = ADAPTERS[cfg.get("adapter", "default")]
    items = []
    for t in traces:
        it = adapter.judge_item(t)
        if it:
            items.append({"item": t["trace_id"], "case": t["trace_id"], "tags": [f"source:{source(t, adapter)}"],
                          **it})
    if not items:
        return "No judgeable runs this week.", "Not run this week (no judged runs)."
    rng = random.Random(seed)
    prod = [i for i in items if "source:production" in i["tags"]]
    rest = [i for i in items if "source:production" not in i["tags"]]
    rng.shuffle(rest)
    sample = (prod + rest)[:cfg.get("judge_sample", 40)]
    (out / "judge_items.jsonl").write_text("".join(json.dumps(i) + "\n" for i in sample))
    fields = cfg.get("judge_fields") or [k for k in sample[0] if k not in ("item", "case", "tags")]
    _agentstats("grade", str(out / "judge_items.jsonl"), "--rubric", str(ROOT / cfg["rubric"]),
                "--model", os.environ.get("AGENTSTATS_JUDGE_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash"),
                "--base-url", "https://api.together.xyz/v1", "--api-key-env", "TOGETHER_API_KEY",
                "--fields", ",".join(fields), "--repeats", "2",
                "--out", str(out / "judge_labels.jsonl"), "--rows", str(out / "judge_rows.jsonl"))
    jrows = [json.loads(x) for x in (out / "judge_rows.jsonl").read_text().splitlines() if x.strip()]
    judged = [r for r in jrows if r["passed"] is not None]
    per_q = Counter(k for r in judged for k, v in r["checks"].items() if not v)
    flip = re.search(r"flip rate ([\d.]+)%", _agentstats("judge", str(out / "judge_labels.jsonl")))
    flip_rate = float(flip.group(1)) / 100 if flip else None
    summary = (f"{len(judged)} of {len(jrows)} sampled runs judged; {sum(r['passed'] for r in judged)} passed every "
               "rubric question. Failed questions: "
               + (", ".join(f"`{k}` {v}" for k, v in per_q.most_common()) or "none")
               + f". Judge flip rate over 2 repeats: {_pct(flip_rate)}.")
    if flip_rate is not None and flip_rate > MAX_JUDGE_FLIPS:
        summary = (f"**The judge is unreliable this week** (flip rate {_pct(flip_rate)} over 2 repeats, above "
                   f"{_pct(MAX_JUDGE_FLIPS)}): treat its verdicts as noise and fix the rubric before trusting them. "
                   + summary)
    n = cfg.get("review_n", 20)
    _agentstats("review", "sample", str(out / "judge_rows.jsonl"), "--n", str(n), "--stratify-by", "source",
                "--seed", str(seed), "--out", str(out / "review"))
    return summary, (f"{n} judged runs sampled for review (blind sheet `{cfg['key']}/review/review.csv` in the "
                     f"artifact; verdicts and sampling weights in `{cfg['key']}/review/key.jsonl`).")


# ---- 6. report -------------------------------------------------------------------------------------------------

def render_agent(cfg: dict, rows: list[dict], rule_summary: dict, judge_summary: str, review_note: str,
                 promote_table: str, failures: list[str]) -> list[str]:
    by_src = Counter(_tag(r, "source") for r in rows)
    lines = [f"## {cfg['agent']}", ""]
    if cfg.get("description"):
        lines += [cfg["description"], ""]
    if not rows:
        return lines + ["No runs this week.", ""]
    lines += [f"{len(rows)} runs: " + ", ".join(f"{k} {v}" for k, v in sorted(by_src.items())) + "."]
    fl = flags(rule_summary)
    lines += ["", "**Needs attention**", ""] + ([f"- {f}" for f in fl] or
                                              [f"- Nothing: every behaviour rule is at or above {_pct(RULE_FLOOR)}."])
    lines += ["", "| source / model | runs | errored (infrastructure) | no usable answer | cost per run |",
              "|---|---|---|---|---|"]
    for key, s in rule_summary.items():
        cpr = f"${s['cost_per_run']:.4f}" if s.get("cost_per_run") else "n/a"
        lines.append(f"| {key} | {s['runs']} | {s['infra']} | {s['no_answer']} | {cpr} |")
    errors = Counter(r["infra_error"] for r in rows if r.get("infra_error"))
    if errors:
        lines += ["", "Errored runs are kept out of the rule rates. Causes: "
                  + "; ".join(f"{msg} ({n})" for msg, n in errors.most_common(3)) + "."]
    checked = [(k, rule, iv) for k, s in rule_summary.items() for rule, iv in sorted(s["checks"].items())]
    if checked:
        lines += ["", "| source / model | rule | runs checked | held | 95% CI |", "|---|---|---|---|---|"]
        lines += [f"| {k} | `{rule}` | {iv['n']} | {_pct(iv['estimate'])} | {_pct(iv['low'])} to {_pct(iv['high'])} |"
                  for k, rule, iv in checked]
    else:
        lines += ["", "No behaviour rule applied to this week's runs."]
    if failures:
        lines += ["", "Runs that broke a rule (trace ids, for Langfuse): " + ", ".join(f"`{t}`" for t in failures[:15])
                  + (f" and {len(failures) - 15} more" if len(failures) > 15 else "")]
    if cfg.get("rubric"):
        lines += ["", "**Rubric judge**", "", judge_summary or "Not run this week (run with `--judge`).",
                  "", "**Human review**", "", review_note]
    lines += ["", "**Candidate eval cases (production failures, deduplicated)**", "", promote_table or "None this week.",
              ""]
    return lines


def render(start, end, sections: list[list[str]]) -> str:
    lines = [f"# agentstats weekly, {start:%Y-%m-%d} to {end:%Y-%m-%d}", ""]
    for s in sections:
        lines += s
    lines += ["## How to close the loop", "",
              "1. Download the `agentstats-weekly` artifact from this run (one folder per agent).",
              "2. Fill `<agent>/review/review.csv` (human_verdict: pass / fail / unsure), then `agentstats review "
              "import <agent>/review/review.csv --key <agent>/review/key.jsonl --rater <you> --out "
              "app/evals/agentstats/<agent>_review_labels.jsonl` and commit the labels.",
              "3. In `<agent>/candidates.jsonl`, set `\"approved\": true` on cases worth keeping, then `agentstats "
              "promote accept <agent>/candidates.jsonl --into app/evals/agentstats/<agent>_cases.jsonl`, and add their "
              "events with your label to that agent's eval set.", "",
              "_Generated by `scripts/agentstats_weekly.py` with agentstats; agents are configured in "
              "`config/agentstats/agents/`. No raw event text is included here; it's in the artifact and Langfuse._"]
    return "\n".join(lines) + "\n"


# ---- main ------------------------------------------------------------------------------------------------------

def run_agent(cfg: dict, traces: list[dict], out: Path, use_judge: bool, seed: int) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    rows = [to_row(t, cfg) for t in traces]
    (out / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    if not rows:
        return render_agent(cfg, rows, {}, "", "", "", [])
    rule_summary = summarize_rules(rows)
    (out / "rules.json").write_text(json.dumps(rule_summary, indent=1))
    failures = [r["case"] for r in rows if (r["checks"] and not all(r["checks"].values())) or r.get("no_answer")]
    judge_summary, review_note = "", "Not run this week (needs the judge: `--judge`)."
    if use_judge and cfg.get("rubric"):
        judge_summary, review_note = judge(cfg, traces, out, seed)
    prod_rows = [r for r in rows if "source:production" in r["tags"]]
    table = ""
    if prod_rows:
        (out / "production_rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in prod_rows))
        args = ["promote", "find", str(out / "production_rows.jsonl"), "--source", "production", "--label-from",
                "human", "--out", str(out / "candidates.jsonl")]
        if cfg.get("group_by"):
            args += ["--group-by", cfg["group_by"]]
        table = "\n".join(ln for ln in _agentstats(*args).splitlines() if ln.startswith("|"))
    return render_agent(cfg, rows, rule_summary, judge_summary, review_note, table, failures)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly agent monitoring with agentstats.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--agents", help="comma-separated config names (default: every config)")
    ap.add_argument("--judge", action="store_true", help="run each agent's rubric judge on a sample (TOGETHER_API_KEY)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    configs = load_configs(args.agents.split(",") if args.agents else None)
    end = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = end - dt.timedelta(days=args.days)
    args.out.mkdir(parents=True, exist_ok=True)
    tools = fetch_observations("TOOL", start, end)
    # LLM calls are named by whichever path made them, so take every generation in the window and match by trace id.
    generations = fetch_observations("GENERATION", start, end, fields="core,model,usage", max_rows=200_000)
    sections = []
    for cfg in configs:
        agents = fetch_observations("AGENT", start, end, name=cfg["agent"])
        traces = join_traces(agents, tools, generations)
        print(f"{cfg['agent']}: {len(traces)} runs")
        sections.append(run_agent(cfg, traces, args.out / cfg["key"], args.judge, args.seed))
    (args.out / "report.md").write_text(render(start, end, sections))
    print(f"wrote {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
