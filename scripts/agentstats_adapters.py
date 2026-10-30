"""
Per-agent logic for scripts/agentstats_weekly.py that a config file can't express. Most agents need none: the
default adapter reads the compact input/output that tracing.trace_entry() records. An agent's config names its
adapter with "adapter": "<name>" (default "default").

An adapter can provide:
  conditions   {name: fn(trace) -> bool}     for a rule's "when"
  checks       fn(trace) -> {rule: bool}     checks that need code (only include a rule when it applies)
  parse_output fn(trace) -> dict | None      the agent's answer; None means no usable answer (parse_error)
  tags         fn(trace) -> [str]            extra tags, e.g. error_type:<x>
  judge_item   fn(trace) -> dict | None      fields the rubric judge sees
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIAGE_PROMPT = ROOT / "app" / "agents" / "triage.py"
COUNT_RE = re.compile(r"^\s*(\d+)\s+occurrences?\b", re.I)


def as_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def event_text(agent_obs: dict) -> str:
    inp = as_json(agent_obs.get("input"))
    if isinstance(inp, dict):
        inp = inp.get("user_input", "")
    return str(inp or "")


def event_field(text: str, field: str) -> str | None:
    m = re.search(rf"^\s*{field}\s*:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


# ---- default: agents traced with trace_entry() (compact dict input and output) -----------------------------

class Default:
    conditions: dict = {}

    def checks(self, trace: dict) -> dict[str, bool]:
        return {}

    def parse_output(self, trace: dict) -> dict | None:
        out = as_json(trace["agent"].get("output"))
        return out if isinstance(out, dict) else None

    def tags(self, trace: dict) -> list[str]:
        inp = as_json(trace["agent"].get("input"))
        et = inp.get("error_type") if isinstance(inp, dict) else None
        return [f"error_type:{et}"] if et else []

    def log_group(self, trace: dict) -> str | None:
        return None

    def no_answer_reason(self, trace: dict) -> str:
        """Why a run has no usable answer (only asked when parse_output returned None)."""
        return "parse_error"

    def judge_item(self, trace: dict) -> dict | None:
        out = self.parse_output(trace)
        if out is None:
            return None
        return {"agent_input": json.dumps(as_json(trace["agent"].get("input")))[:1500],
                "agent_steps": "\n".join(f"{c['name']}: {str(c['output'])[:200]}" for c in trace["calls"])[:2000]
                or "no recorded steps",
                "agent_output": json.dumps(out)[:1500]}


# ---- TriageAgent: traced through BaseAgent.run(); answer is JSON inside the ReAct answer --------------------

class Triage(Default):
    def log_group(self, trace: dict) -> str | None:
        for c in trace["calls"]:
            if c["name"] == "get_occurrence_count" and isinstance(c["input"], dict) and c["input"].get("log_group"):
                return str(c["input"]["log_group"])
        lg = event_field(event_text(trace["agent"]), "log_group")
        return lg if lg and lg.lower() not in ("none", "null", "") else None

    @property
    def conditions(self) -> dict:
        return {"log_group": lambda t: self.log_group(t) is not None}

    def parse_output(self, trace: dict) -> dict | None:
        out = as_json(trace["agent"].get("output"))
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

    @staticmethod
    def looked_up_counts(trace: dict) -> list[int]:
        out = []
        for c in trace["calls"]:
            if c["name"] == "get_occurrence_count":
                m = COUNT_RE.match(c["output"])
                if m:
                    out.append(int(m.group(1)))
        return out

    @staticmethod
    def count_supported(stated: int, looked_up: list[int]) -> bool:
        """The stated count is one the tool returned, or 1 when every lookup found none (the event being triaged
        is itself one occurrence; the agent may reasonably treat the log count as a lower bound)."""
        return stated in looked_up or (stated == 1 and all(n == 0 for n in looked_up))

    def checks(self, trace: dict) -> dict[str, bool]:
        looked_up = self.looked_up_counts(trace)
        stated = (self.parse_output(trace) or {}).get("occurrences_24h")
        if looked_up and isinstance(stated, (int, float)) and not isinstance(stated, bool):
            return {"count_matches_lookup": self.count_supported(int(stated), looked_up)}
        return {}

    def tags(self, trace: dict) -> list[str]:
        d = self.parse_output(trace) or {}
        et = event_field(event_text(trace["agent"]), "error_type") or "unknown"
        return [f"error_type:{et}", f"severity:{d.get('severity', 'none')}", f"decision:{d.get('decision', 'none')}"]

    @staticmethod
    def severity_guide() -> str:
        return "\n".join(ln.strip() for ln in TRIAGE_PROMPT.read_text().splitlines() if re.match(r"^\s*P[0-3]\s+—", ln))

    def judge_item(self, trace: dict) -> dict | None:
        d = self.parse_output(trace)
        if d is None:
            return None
        text = event_text(trace["agent"])
        event = "\n".join(f"{f}: {(event_field(text, f) or '')[:600]}"
                          for f in ("error_type", "title", "service", "description"))
        tools = "\n".join(f"{c['name']}: {c['output'][:300]}" for c in trace["calls"]) or "no tool calls"
        return {"severity_guide": self.severity_guide(), "error_event": event, "tool_results": tools,
                "agent_decision": json.dumps({k: d.get(k) for k in ("decision", "severity", "occurrences_24h",
                                                                    "reasoning")})}


# ---- DiagnosisAgent: the diagnosis is the input of the last submit_diagnosis call the tool accepted -----------

class Diagnosis(Default):
    """The grounding gate (PR #211) rejects an ungrounded submit_diagnosis ("REJECTED — fix the following ...") and
    the agent resubmits; the accepted call's input is the diagnosis."""

    @staticmethod
    def submissions(trace: dict) -> list[dict]:
        return [c for c in trace["calls"] if c["name"] == "submit_diagnosis"]

    def parse_output(self, trace: dict) -> dict | None:
        accepted = [c for c in self.submissions(trace) if c["output"].startswith("Diagnosis accepted")]
        return accepted[-1]["input"] if accepted and isinstance(accepted[-1]["input"], dict) else None

    def checks(self, trace: dict) -> dict[str, bool]:
        subs = self.submissions(trace)
        if not subs:
            return {}
        return {"diagnosis_accepted": self.parse_output(trace) is not None,
                "accepted_first_try": subs[0]["output"].startswith("Diagnosis accepted")}

    def log_group(self, trace: dict) -> str | None:
        lg = event_field(event_text(trace["agent"]), "log_group")
        return lg if lg and lg.lower() not in ("none", "null", "") else None

    def no_answer_reason(self, trace: dict) -> str:
        out = str(as_json(trace["agent"].get("output")) or "")
        if "allowed number of steps" in out:
            return "step_limit"                   # ran out of steps (MAX_ITERATIONS) before an accepted submission
        return "not_accepted" if self.submissions(trace) else "no_submission"

    def tags(self, trace: dict) -> list[str]:
        d = self.parse_output(trace) or {}
        et = event_field(event_text(trace["agent"]), "error_type") or "unknown"
        return [f"error_type:{et}", f"confidence:{d.get('confidence', 'none')}"]

    def judge_item(self, trace: dict) -> dict | None:
        d = self.parse_output(trace)
        if d is None:
            return None
        text = event_text(trace["agent"])
        incident = "\n".join(f"{f}: {(event_field(text, f) or '')[:600]}"
                             for f in ("error_type", "title", "service", "description"))
        reads = [c for c in trace["calls"] if c["name"] in ("get_file_contents", "grep_codebase", "find_callers",
                                                             "verify_symbol_in_repo")]
        target = str(d.get("affected_file") or "")
        # Reads of the file the diagnosis names first (that's where its support is), then the most recent others.
        ranked = sorted(reads, key=lambda c: target and target in json.dumps(c["input"]), reverse=True)[:6]
        code = "\n\n".join(f"{c['name']} {json.dumps(c['input'])[:150]}:\n{c['output'][:1200]}" for c in ranked)
        diag = {k: str(d.get(k, ""))[:800] for k in ("root_cause", "root_cause_snippet", "affected_file",
                                                     "affected_function", "evidence", "fix_approach", "confidence")}
        return {"incident": incident, "code_the_agent_read": code or "no code read",
                "diagnosis": json.dumps(diag)}


ADAPTERS = {"default": Default(), "triage": Triage(), "diagnosis": Diagnosis()}
