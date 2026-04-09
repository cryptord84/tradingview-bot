"""Backtest Scorer — Statistical strategy comparison using bootstrap CI + Mann-Whitney U.

Adapted from the Growth Engine experiment framework for trading strategy evaluation.
Compares strategy variants (parameter sets, different indicators, etc.) using
non-parametric statistics that don't assume normal returns.

Usage:
  from app.services.backtest_scorer import get_backtest_scorer

  scorer = get_backtest_scorer()

  # Create a comparison: RSI length 14 vs 21
  scorer.create_experiment(
      name="RSI length optimization",
      baseline="RSI-14",
      variants=["RSI-21", "RSI-10"],
      metric="pnl_usd",
  )

  # Log trades as they come in (from dry-run or backtest)
  scorer.log_trade("RSI length optimization", variant="RSI-14", metrics={"pnl_usd": 12.50, ...})
  scorer.log_trade("RSI length optimization", variant="RSI-21", metrics={"pnl_usd": 18.30, ...})

  # Score when ready
  result = scorer.score("RSI length optimization")
  # -> { status: "keep", winner: "RSI-21", lift: 46.4%, p: 0.023, ci_95: [12.1, 81.2] }

  # Import historical dry-run trades
  scorer.import_dry_run_trades()

  # View the playbook of proven winners
  scorer.get_playbook()
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

import numpy as np
from scipy import stats

logger = logging.getLogger("bot.backtest_scorer")

# ── Configuration ─────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).parent.parent.parent / "data" / "backtest_scorer"

# Statistical thresholds — adapted from Growth Engine
BOOTSTRAP_ITERATIONS = int(os.environ.get("BACKTEST_BOOTSTRAP_ITER", "1000"))
P_WINNER = float(os.environ.get("BACKTEST_P_WINNER", "0.05"))
P_TREND = float(os.environ.get("BACKTEST_P_TREND", "0.10"))
LIFT_WIN = float(os.environ.get("BACKTEST_LIFT_WIN", "15.0"))  # min % lift to declare winner
MIN_TRADES = int(os.environ.get("BACKTEST_MIN_TRADES", "30"))  # per variant

# Available metrics for comparison
METRICS = {
    "pnl_usd": "Per-trade P&L in USD",
    "pnl_pct": "Per-trade P&L as percentage",
    "win": "Binary win (1) or loss (0) — compares win rates",
    "rr_ratio": "Risk-reward ratio achieved",
    "profit_factor": "Gross profit / gross loss per trade window",
    "sharpe_trade": "Per-trade Sharpe approximation",
}


# ── Statistical Functions ─────────────────────────────────────────────────────


def bootstrap_lift_ci(
    baseline_vals: list[float],
    variant_vals: list[float],
    n_iter: int = BOOTSTRAP_ITERATIONS,
    ci: int = 95,
) -> tuple[Optional[float], Optional[float]]:
    """Bootstrap confidence interval for lift = (mean(variant) - mean(baseline)) / mean(baseline) * 100.

    Returns (lower_bound, upper_bound) as percentages, or (None, None) if baseline mean is zero.
    Uses non-parametric resampling — no normality assumption on returns.
    """
    a = np.array(baseline_vals, dtype=float)
    b = np.array(variant_vals, dtype=float)
    rng = np.random.default_rng(42)
    lifts = []
    for _ in range(n_iter):
        sa = rng.choice(a, size=len(a), replace=True)
        sb = rng.choice(b, size=len(b), replace=True)
        baseline_mean = sa.mean()
        if baseline_mean == 0:
            continue
        lifts.append((sb.mean() - baseline_mean) / baseline_mean * 100)
    if not lifts:
        return None, None
    lo = float(np.percentile(lifts, (100 - ci) / 2))
    hi = float(np.percentile(lifts, 100 - (100 - ci) / 2))
    return round(lo, 2), round(hi, 2)


def compare_distributions(
    baseline_vals: list[float],
    variant_vals: list[float],
) -> dict:
    """Run full statistical comparison between two trade result sets.

    Returns dict with lift, p-values, bootstrap CI, and descriptive stats.
    """
    a = np.array(baseline_vals, dtype=float)
    b = np.array(variant_vals, dtype=float)

    a_mean = float(a.mean())
    b_mean = float(b.mean())
    lift = ((b_mean - a_mean) / a_mean * 100) if a_mean != 0 else 0.0

    # Mann-Whitney U — non-parametric, handles fat-tailed trading returns
    _, p_two = stats.mannwhitneyu(a, b, alternative="two-sided")
    _, p_less = stats.mannwhitneyu(a, b, alternative="less")  # H1: variant > baseline

    ci_lo, ci_hi = bootstrap_lift_ci(a.tolist(), b.tolist())

    # Descriptive stats
    def desc(arr):
        return {
            "mean": round(float(arr.mean()), 4),
            "median": round(float(np.median(arr)), 4),
            "std": round(float(arr.std()), 4),
            "min": round(float(arr.min()), 4),
            "max": round(float(arr.max()), 4),
            "n": len(arr),
            "win_rate": round(float((arr > 0).sum() / len(arr) * 100), 1) if len(arr) > 0 else 0,
        }

    return {
        "lift_pct": round(lift, 2),
        "p_value_one_sided": round(float(p_less), 4),
        "p_value_two_sided": round(float(p_two), 4),
        "ci_95": [ci_lo, ci_hi],
        "baseline_stats": desc(a),
        "variant_stats": desc(b),
    }


# ── Experiment Manager ────────────────────────────────────────────────────────


class BacktestScorer:
    """Manages strategy comparison experiments with statistical rigor."""

    def __init__(self):
        self._lock = Lock()
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._experiments_file = _DATA_DIR / "experiments.json"
        self._playbook_file = _DATA_DIR / "playbook.json"
        self._experiments: list[dict] = self._load_json(self._experiments_file, [])
        self._playbook: dict = self._load_json(self._playbook_file, {})
        logger.info(
            f"Backtest scorer loaded: {len(self._experiments)} experiments, "
            f"{len(self._playbook)} playbook entries"
        )

    @staticmethod
    def _load_json(path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return default

    def _save_experiments(self):
        self._experiments_file.write_text(
            json.dumps(self._experiments, indent=2, default=str)
        )

    def _save_playbook(self):
        self._playbook_file.write_text(
            json.dumps(self._playbook, indent=2, default=str)
        )

    def _find_experiment(self, name: str) -> Optional[dict]:
        for exp in self._experiments:
            if exp["name"] == name:
                return exp
        return None

    # ── Experiment Lifecycle ──────────────────────────────────────────────

    def create_experiment(
        self,
        name: str,
        baseline: str,
        variants: list[str],
        metric: str = "pnl_usd",
        min_trades: int = MIN_TRADES,
        hypothesis: str = "",
    ) -> dict:
        """Create a new strategy comparison experiment.

        Args:
            name: Descriptive name (e.g. "RSI length optimization")
            baseline: Name of the baseline variant (e.g. "RSI-14")
            variants: List of variant names to compare against baseline
            metric: Primary metric to compare (pnl_usd, win, rr_ratio, etc.)
            min_trades: Minimum trades per variant before scoring
            hypothesis: What you expect to find
        """
        with self._lock:
            existing = self._find_experiment(name)
            if existing:
                raise ValueError(f"Experiment '{name}' already exists (status: {existing['status']})")

            if len(variants) > 10:
                variants = variants[:10]
                logger.warning("Capped variants at 10")

            experiment = {
                "name": name,
                "baseline": baseline,
                "variants": variants,
                "all_variants": [baseline] + variants,
                "metric": metric,
                "min_trades": min_trades,
                "hypothesis": hypothesis,
                "status": "running",  # running -> trending -> keep/discard
                "created_at": datetime.now(timezone.utc).isoformat(),
                "trades": [],  # [{variant, metrics, timestamp}, ...]
                "result": None,
                "winner": None,
            }
            self._experiments.append(experiment)
            self._save_experiments()

        logger.info(
            f"Created experiment '{name}': {baseline} vs {variants} on {metric} "
            f"(min {min_trades} trades/variant)"
        )
        return experiment

    def log_trade(
        self,
        experiment_name: str,
        variant: str,
        metrics: dict,
        timestamp: Optional[str] = None,
    ) -> dict:
        """Log a single trade result for a variant.

        Args:
            experiment_name: Name of the experiment
            variant: Which variant produced this trade
            metrics: Dict of metric values, must include the experiment's primary metric.
                     e.g. {"pnl_usd": 12.50, "win": 1, "rr_ratio": 2.1}
            timestamp: ISO timestamp (defaults to now)
        """
        with self._lock:
            exp = self._find_experiment(experiment_name)
            if not exp:
                raise ValueError(f"Experiment '{experiment_name}' not found")
            if exp["status"] not in ("running", "trending"):
                raise ValueError(f"Experiment '{experiment_name}' is already {exp['status']}")
            if variant not in exp["all_variants"]:
                raise ValueError(
                    f"Unknown variant '{variant}'. Expected one of: {exp['all_variants']}"
                )
            if exp["metric"] not in metrics:
                raise ValueError(
                    f"Missing primary metric '{exp['metric']}' in trade metrics. "
                    f"Got: {list(metrics.keys())}"
                )

            entry = {
                "variant": variant,
                "metrics": metrics,
                "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            }
            exp["trades"].append(entry)
            self._save_experiments()

        return entry

    def log_trades_bulk(
        self,
        experiment_name: str,
        trades: list[dict],
    ) -> int:
        """Bulk-log trades. Each dict must have 'variant' and 'metrics' keys.
        Returns count of trades logged.
        """
        count = 0
        with self._lock:
            exp = self._find_experiment(experiment_name)
            if not exp:
                raise ValueError(f"Experiment '{experiment_name}' not found")
            for t in trades:
                variant = t["variant"]
                metrics = t["metrics"]
                if variant not in exp["all_variants"]:
                    continue
                if exp["metric"] not in metrics:
                    continue
                exp["trades"].append({
                    "variant": variant,
                    "metrics": metrics,
                    "timestamp": t.get("timestamp", datetime.now(timezone.utc).isoformat()),
                })
                count += 1
            self._save_experiments()
        return count

    def score(self, experiment_name: str) -> dict:
        """Score an experiment — compare all variants against baseline.

        Returns the scoring result with status, winner (if any), statistical details.
        Auto-promotes winners to the playbook.
        """
        with self._lock:
            exp = self._find_experiment(experiment_name)
            if not exp:
                raise ValueError(f"Experiment '{experiment_name}' not found")

            metric = exp["metric"]
            min_trades = exp["min_trades"]

            # Group trades by variant
            variant_data: dict[str, list[float]] = {}
            for trade in exp["trades"]:
                v = trade["variant"]
                val = trade["metrics"].get(metric, 0)
                variant_data.setdefault(v, []).append(float(val))

            # Check sample sizes
            insufficient = []
            for v in exp["all_variants"]:
                n = len(variant_data.get(v, []))
                if n < min_trades:
                    insufficient.append((v, n))

            if insufficient:
                # Check for early trending signal (need at least 15 trades)
                baseline_vals = variant_data.get(exp["baseline"], [])
                trending_found = None

                if len(baseline_vals) >= 15:
                    best_p = 1.0
                    for v in exp["variants"]:
                        vals = variant_data.get(v, [])
                        if len(vals) < 15:
                            continue
                        _, p = stats.mannwhitneyu(baseline_vals, vals, alternative="less")
                        if p < P_TREND and p < best_p:
                            best_p = p
                            trending_found = v

                if trending_found:
                    exp["status"] = "trending"
                    self._save_experiments()
                    t_vals = variant_data[trending_found]
                    b_mean = np.mean(baseline_vals)
                    lift = ((np.mean(t_vals) - b_mean) / b_mean * 100) if b_mean else 0
                    return {
                        "status": "trending",
                        "trending_variant": trending_found,
                        "p_value": round(float(best_p), 4),
                        "preliminary_lift_pct": round(float(lift), 2),
                        "message": f"'{trending_found}' showing early promise (p={best_p:.3f}, lift={lift:.1f}%). Need more trades to confirm.",
                        "sample_counts": {v: len(variant_data.get(v, [])) for v in exp["all_variants"]},
                        "min_trades_required": min_trades,
                    }

                return {
                    "status": "insufficient_data",
                    "sample_counts": {v: len(variant_data.get(v, [])) for v in exp["all_variants"]},
                    "min_trades_required": min_trades,
                    "message": f"Need at least {min_trades} trades per variant. "
                               + ", ".join(f"'{v}': {n}/{min_trades}" for v, n in insufficient),
                }

            baseline_vals = variant_data[exp["baseline"]]

            # Evaluate each variant against baseline
            variant_results = []
            for v in exp["variants"]:
                vals = variant_data.get(v, [])
                if not vals:
                    continue

                comparison = compare_distributions(baseline_vals, vals)

                # Classify
                p = comparison["p_value_one_sided"]
                lift = comparison["lift_pct"]
                p_two = comparison["p_value_two_sided"]

                if p < P_WINNER and lift >= LIFT_WIN:
                    status = "keep"
                elif p_two < P_WINNER and lift < 0:
                    status = "crash" if lift <= -LIFT_WIN else "discard"
                elif p < P_TREND:
                    status = "trending"
                else:
                    status = "no_effect"

                variant_results.append({
                    "variant": v,
                    "status": status,
                    **comparison,
                })

            # Determine overall experiment outcome
            winners = [r for r in variant_results if r["status"] == "keep"]
            crashes = [r for r in variant_results if r["status"] in ("crash", "discard")]

            if winners:
                best = max(winners, key=lambda r: r["lift_pct"])
                exp["status"] = "keep"
                exp["winner"] = best["variant"]
            elif all(r["status"] in ("crash", "discard", "no_effect") for r in variant_results):
                exp["status"] = "discard"
            else:
                exp["status"] = "trending" if any(r["status"] == "trending" for r in variant_results) else "running"

            result = {
                "status": exp["status"],
                "winner": exp.get("winner"),
                "baseline": {
                    "variant": exp["baseline"],
                    "n": len(baseline_vals),
                    "mean": round(float(np.mean(baseline_vals)), 4),
                    "median": round(float(np.median(baseline_vals)), 4),
                    "win_rate": round(float((np.array(baseline_vals) > 0).sum() / len(baseline_vals) * 100), 1),
                },
                "variants": variant_results,
                "thresholds": {
                    "p_winner": P_WINNER,
                    "p_trend": P_TREND,
                    "lift_pct_required": LIFT_WIN,
                    "min_trades": min_trades,
                },
                "scored_at": datetime.now(timezone.utc).isoformat(),
            }

            exp["result"] = result
            self._save_experiments()

            # Auto-promote winners to playbook
            if winners:
                best = max(winners, key=lambda r: r["lift_pct"])
                self._playbook[experiment_name] = {
                    "winner": best["variant"],
                    "baseline": exp["baseline"],
                    "metric": metric,
                    "lift_pct": best["lift_pct"],
                    "p_value": best["p_value_one_sided"],
                    "ci_95": best["ci_95"],
                    "baseline_mean": result["baseline"]["mean"],
                    "winner_mean": best["variant_stats"]["mean"],
                    "winner_win_rate": best["variant_stats"]["win_rate"],
                    "trades_analyzed": best["baseline_stats"]["n"] + best["variant_stats"]["n"],
                    "promoted_at": datetime.now(timezone.utc).isoformat(),
                    "hypothesis": exp.get("hypothesis", ""),
                }
                self._save_playbook()
                logger.info(
                    f"WINNER: '{best['variant']}' beats '{exp['baseline']}' "
                    f"with {best['lift_pct']:.1f}% lift (p={best['p_value_one_sided']:.4f})"
                )

        return result

    # ── Import from Dry-Run Manager ───────────────────────────────────────

    def import_dry_run_trades(
        self,
        experiment_name: str,
        variant_map: Optional[dict[str, str]] = None,
    ) -> int:
        """Import trades from the dry-run trade log into an experiment.

        Args:
            experiment_name: Target experiment name
            variant_map: Maps strategy names in dry-run logs to variant names.
                         e.g. {"RSI DIVERGENCE V1.0": "RSI-Div", "VOLUME MOMENTUM V1.0": "Vol-Mom"}
                         If None, uses the strategy name directly as the variant name.

        Returns count of trades imported.
        """
        dry_run_file = Path(__file__).parent.parent.parent / "data" / "dry_run_trades.json"
        if not dry_run_file.exists():
            logger.warning("No dry-run trades file found")
            return 0

        try:
            trades = json.loads(dry_run_file.read_text())
        except Exception as e:
            logger.error(f"Failed to read dry-run trades: {e}")
            return 0

        mapped_trades = []
        for t in trades:
            strategy = t.get("strategy", "")
            variant = (variant_map or {}).get(strategy, strategy) if variant_map else strategy

            pnl = t.get("simulated_pnl", 0)
            entry_price = t.get("entry_price", 0)
            tp_price = t.get("tp_price")
            sl_price = t.get("sl_price")
            rr = t.get("expected_rr")

            metrics = {
                "pnl_usd": pnl,
                "win": 1 if pnl > 0 else 0,
                "confidence": t.get("confidence", 0),
                "entry_price": entry_price,
                "trade_usd": t.get("trade_usd", 0),
            }
            if rr is not None:
                metrics["rr_ratio"] = rr
            if entry_price and tp_price and sl_price:
                tp_pct = abs(tp_price - entry_price) / entry_price * 100
                sl_pct = abs(sl_price - entry_price) / entry_price * 100
                metrics["pnl_pct"] = tp_pct if pnl > 0 else -sl_pct

            mapped_trades.append({
                "variant": variant,
                "metrics": metrics,
                "timestamp": t.get("timestamp", ""),
            })

        if not mapped_trades:
            return 0

        return self.log_trades_bulk(experiment_name, mapped_trades)

    # ── Quick Compare (no experiment needed) ──────────────────────────────

    def quick_compare(
        self,
        baseline_values: list[float],
        variant_values: list[float],
        baseline_label: str = "Baseline",
        variant_label: str = "Variant",
    ) -> dict:
        """One-shot statistical comparison without creating a persistent experiment.

        Pass two lists of per-trade metric values and get back the full analysis.
        Useful for ad-hoc comparisons.
        """
        if len(baseline_values) < 5 or len(variant_values) < 5:
            return {"error": "Need at least 5 data points per group"}

        comparison = compare_distributions(baseline_values, variant_values)

        # Classify
        p = comparison["p_value_one_sided"]
        lift = comparison["lift_pct"]
        p_two = comparison["p_value_two_sided"]

        if p < P_WINNER and lift >= LIFT_WIN:
            verdict = "WINNER"
        elif p_two < P_WINNER and lift < 0:
            verdict = "LOSER" if lift <= -LIFT_WIN else "WORSE"
        elif p < P_TREND:
            verdict = "TRENDING"
        else:
            verdict = "NO_EFFECT"

        return {
            "verdict": verdict,
            "baseline_label": baseline_label,
            "variant_label": variant_label,
            **comparison,
            "interpretation": _interpret(verdict, variant_label, baseline_label, lift, p, comparison["ci_95"]),
        }

    # ── Playbook & Listing ────────────────────────────────────────────────

    def get_playbook(self) -> dict:
        """Return all proven winners — the accumulated knowledge from experiments."""
        with self._lock:
            return dict(self._playbook)

    def list_experiments(self, status: Optional[str] = None) -> list[dict]:
        """List experiments, optionally filtered by status."""
        with self._lock:
            exps = []
            for exp in self._experiments:
                if status and exp["status"] != status:
                    continue
                # Summary without full trade list
                trade_counts = {}
                for t in exp["trades"]:
                    v = t["variant"]
                    trade_counts[v] = trade_counts.get(v, 0) + 1
                exps.append({
                    "name": exp["name"],
                    "status": exp["status"],
                    "metric": exp["metric"],
                    "baseline": exp["baseline"],
                    "variants": exp["variants"],
                    "trade_counts": trade_counts,
                    "min_trades": exp["min_trades"],
                    "winner": exp.get("winner"),
                    "created_at": exp["created_at"],
                })
            return exps

    def delete_experiment(self, name: str) -> bool:
        """Remove an experiment by name."""
        with self._lock:
            for i, exp in enumerate(self._experiments):
                if exp["name"] == name:
                    self._experiments.pop(i)
                    self._save_experiments()
                    return True
        return False

    def get_experiment_detail(self, name: str) -> Optional[dict]:
        """Get full experiment detail including all trades."""
        with self._lock:
            return self._find_experiment(name)


def _interpret(verdict, variant, baseline, lift, p, ci) -> str:
    """Human-readable interpretation of the result."""
    ci_str = f"[{ci[0]}%, {ci[1]}%]" if ci[0] is not None else "[N/A]"
    if verdict == "WINNER":
        return (
            f"'{variant}' significantly outperforms '{baseline}' with "
            f"{lift:.1f}% lift (p={p:.4f}, 95% CI: {ci_str}). "
            f"Promote to live trading."
        )
    elif verdict == "LOSER":
        return (
            f"'{variant}' is significantly worse than '{baseline}' "
            f"({lift:.1f}% lift, p={p:.4f}). Discard this configuration."
        )
    elif verdict == "WORSE":
        return (
            f"'{variant}' underperforms '{baseline}' ({lift:.1f}%, p={p:.4f}). "
            f"Not recommended."
        )
    elif verdict == "TRENDING":
        return (
            f"'{variant}' shows early promise vs '{baseline}' "
            f"({lift:.1f}% lift, p={p:.4f}, 95% CI: {ci_str}). "
            f"Collect more trades to confirm."
        )
    else:
        return (
            f"No statistically significant difference between '{variant}' and "
            f"'{baseline}' (lift={lift:.1f}%, p={p:.4f}). Keep running or discard."
        )


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[BacktestScorer] = None


def get_backtest_scorer() -> BacktestScorer:
    global _instance
    if _instance is None:
        _instance = BacktestScorer()
    return _instance
