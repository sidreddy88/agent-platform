"""
Acceptance rules: when does a candidate harness replace the incumbent?

The rules follow RRSI (Xia et al., "RRSI: Regularized Recursive
Self-Improvement of Agent Harnesses", arXiv 2609.24972; reference
implementation github.com/google-research/rrsi, Apache-2.0,
rrsi/selection.py), reimplemented here for our setting:

  floor       S' >= S* - delta                            (RRSI Eq. floor)
  clear gain  if dS > delta:  dC <= beta0 + beta1 * dS    (RRSI Eq. tokenbudget)
  in band     otherwise:      w_s*dS - w_c*dC > 0         (RRSI Alg. 2 l.5)
  guards      domain vetoes (RRSI's eng domain vetoes a rising no-submission
              rate; ours is the escalation rate, the same failure shape)

dC is RELATIVE (C'/C - 1), as in RRSI. Defaults mirror RRSI's coding config,
where w_s = 0: inside the noise band a candidate can only win by being
cheaper, never by a within-noise score bump. Our noise is far larger than
theirs (delta ~0.2-0.3 on ~17 cases vs their 0.017 on 178 trials), so in
practice most gains are "in band" and the rule reduces to "no worse, and
cheaper".

What differs from RRSI, and why:

- **Paired, per-case comparison instead of one aggregate score each.** The
  candidate and incumbent run on the same cases, so S and C are compared
  case by case, and each decision records which cases improved or
  regressed. With ~20% of cases flipping between identical runs, knowing
  *which* cases moved matters as much as the mean.
- **Calibration from trial pairs.** RRSI estimates delta by re-running the
  unchanged base harness. We get the same null distribution from round 0's
  k=2 trials (trial 1 vs trial 2 of the same harness), bootstrapped over
  cases: no extra run.
- **Guard cases.** The evolve set's always-pass regression guards must keep
  passing: a candidate that breaks the easy path is vetoed outright.
- **No novelty term, pruning window or annealing.** Those need many rounds;
  we can afford a handful. See docs/blog-drafts/harness-evolution-v2.md §0.4.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import asdict, dataclass, field


@dataclass
class CaseResult:
    """One case's outcome across its trials under one harness."""
    passes: int
    trials: int
    escalations: int = 0          # attempts that never got an accepted submission
    cost_usd: float = 0.0         # summed over this case's trials
    verdicts: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.passes / self.trials if self.trials else 0.0


@dataclass
class EvalResult:
    """A harness evaluated on a set of cases. S and C are means over cases."""
    per_case: dict[str, CaseResult]

    @property
    def S(self) -> float:
        return statistics.fmean(c.score for c in self.per_case.values()) if self.per_case else 0.0

    @property
    def C(self) -> float:
        """Mean cost per trial."""
        trials = sum(c.trials for c in self.per_case.values())
        return sum(c.cost_usd for c in self.per_case.values()) / trials if trials else 0.0

    @property
    def escalation_rate(self) -> float:
        trials = sum(c.trials for c in self.per_case.values())
        return sum(c.escalations for c in self.per_case.values()) / trials if trials else 0.0

    def to_json(self) -> dict:
        return {"S": self.S, "C": self.C, "escalation_rate": self.escalation_rate,
                "per_case": {k: asdict(v) for k, v in self.per_case.items()}}

    @classmethod
    def from_json(cls, data: dict) -> EvalResult:
        return cls({k: CaseResult(**v) for k, v in data["per_case"].items()})


@dataclass(frozen=True)
class AcceptanceConfig:
    delta: float = 0.25            # noise band; replaced by calibrate() after round 0
    beta0: float = 0.10            # RRSI coding: 10% more cost for free on a clear gain
    beta1: float = 44.5            # RRSI coding: each +1pp of S buys +44.5% cost
    w_s: float = 0.0               # RRSI coding: no credit for within-band score change
    w_c: float = 15.0              # RRSI coding
    max_escalation_rise: float = 0.05
    guard_cases: tuple[str, ...] = ()


@dataclass
class Decision:
    accept: bool
    reason: str
    delta_S: float
    delta_C: float                                  # relative
    improved: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def paired_deltas(incumbent: EvalResult, candidate: EvalResult) -> tuple[float, float, list, list]:
    """dS over the cases both evaluated, relative dC, and which cases moved."""
    shared = sorted(set(incumbent.per_case) & set(candidate.per_case))
    if not shared:
        raise ValueError("candidate and incumbent share no evaluated cases")
    inc = EvalResult({c: incumbent.per_case[c] for c in shared})
    cand = EvalResult({c: candidate.per_case[c] for c in shared})
    dS = cand.S - inc.S
    dC = (cand.C / inc.C - 1.0) if inc.C > 0 else 0.0
    improved = [c for c in shared if cand.per_case[c].score > inc.per_case[c].score]
    regressed = [c for c in shared if cand.per_case[c].score < inc.per_case[c].score]
    return dS, dC, improved, regressed


def decide(incumbent: EvalResult, candidate: EvalResult, S_star: float,
           cfg: AcceptanceConfig) -> Decision:
    dS, dC, improved, regressed = paired_deltas(incumbent, candidate)
    d = Decision(False, "", dS, dC, improved, regressed)

    for g in cfg.guard_cases:
        cr = candidate.per_case.get(g)
        if cr is not None and cr.passes < cr.trials:
            d.vetoes.append(f"guard case {g} failed {cr.trials - cr.passes}/{cr.trials}")
    rise = candidate.escalation_rate - incumbent.escalation_rate
    if rise > cfg.max_escalation_rise:
        d.vetoes.append(f"escalation rate rose {rise:+.3f} (limit {cfg.max_escalation_rise})")
    if d.vetoes:
        d.reason = "vetoed: " + "; ".join(d.vetoes)
        return d

    floor = S_star - cfg.delta
    if candidate.S < floor:
        d.reason = f"below noise floor: S' {candidate.S:.3f} < S* {S_star:.3f} - delta {cfg.delta:.3f}"
        return d

    if dS > cfg.delta:
        budget = cfg.beta0 + cfg.beta1 * dS
        d.accept = dC <= budget
        d.reason = (f"clear gain dS {dS:+.3f} > delta {cfg.delta:.3f}; relative cost "
                    f"{dC:+.3f} {'<=' if d.accept else '>'} budget {budget:.3f}")
        return d

    shaped = cfg.w_s * dS - cfg.w_c * dC
    d.accept = shaped > 0
    d.reason = (f"within noise band (dS {dS:+.3f}, delta {cfg.delta:.3f}); "
                f"{cfg.w_s}*dS - {cfg.w_c}*dC = {shaped:+.3f} {'>' if d.accept else '<='} 0"
                + ("" if d.accept else " (in band, a candidate must be cheaper)"))
    return d


def calibrate_delta(trial_pairs: dict[str, tuple[bool, bool]], z: float = 2.0,
                    trials_per_eval: int = 1, reps: int = 2000, seed: int = 7) -> float:
    """Noise band from two trials of the SAME harness on each case.

    The null difference of two independent evaluations is observed directly:
    dS_null = mean over cases of (trial1 - trial2). Its standard deviation is
    bootstrapped over cases, and delta = z * sd, so an unchanged harness
    clears the floor about 97.5% of the time at z = 2 (as in rrsi/calibrate.py).

    The pairs measure single-trial evaluations. If each later evaluation
    averages `trials_per_eval` trials per case, its noise shrinks by
    sqrt(trials_per_eval), and so does delta.
    """
    diffs = [float(a) - float(b) for a, b in trial_pairs.values()]
    if len(diffs) < 2:
        raise ValueError("need at least 2 cases with two trials each to calibrate")
    rng = random.Random(seed)
    boot = [statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(reps)]
    return z * statistics.pstdev(boot) / trials_per_eval ** 0.5
