"""Apply the empirical BTCD calibration table at trading time.

Trained by `backtesting/build_btcd_calibrator.py` from accumulated calibration
snapshots. Maps the parametric model's `fair_prob` to an isotonic-regressed
empirical probability that removes the bidirectional overdispersion documented
in `project_btcd_audit_20260511.md`.

Singleton, lazy-loaded on first `apply()` call. Tolerates missing file by
returning the input unchanged (no-op fallback so a missing table doesn't break
the bot — but it logs a one-time warning).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("bot.kalshi.calibrator")

CALIBRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "btcd_calibration.json",
)


class _BTCDCalibrator:
    def __init__(self):
        self._breakpoints: Optional[list[tuple[float, float]]] = None
        self._warned = False
        self._loaded_meta: dict = {}

    def _load(self) -> bool:
        if self._breakpoints is not None:
            return True
        try:
            with open(CALIBRATION_PATH) as f:
                table = json.load(f)
            bps = table.get("breakpoints", [])
            if not bps:
                raise ValueError("breakpoints empty")
            # Ensure sorted by x ascending — paranoia
            bps = sorted([(float(p), float(a)) for p, a in bps], key=lambda b: b[0])
            self._breakpoints = bps
            self._loaded_meta = table.get("meta", {})
            logger.info(
                f"BTCD calibrator loaded: {len(bps)} breakpoints, "
                f"trained on {self._loaded_meta.get('n_samples', '?')} samples"
            )
            return True
        except Exception as e:
            if not self._warned:
                logger.warning(f"BTCD calibrator unavailable ({e}); falling back to raw model probs")
                self._warned = True
            self._breakpoints = []
            return False

    def apply(self, pred: float) -> float:
        """Map raw parametric fair_prob to calibrated probability via linear
        interpolation between learned breakpoints. Returns input unchanged if
        table missing."""
        if self._breakpoints is None:
            self._load()
        if not self._breakpoints:
            return pred
        bps = self._breakpoints
        if pred <= bps[0][0]:
            return bps[0][1]
        if pred >= bps[-1][0]:
            return bps[-1][1]
        # Bracket and interpolate
        for i in range(len(bps) - 1):
            x0, y0 = bps[i]
            x1, y1 = bps[i + 1]
            if x0 <= pred <= x1:
                if x1 == x0:
                    return y0
                t = (pred - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return pred  # unreachable

    def is_loaded(self) -> bool:
        if self._breakpoints is None:
            self._load()
        return bool(self._breakpoints)

    def meta(self) -> dict:
        if self._breakpoints is None:
            self._load()
        return dict(self._loaded_meta)


_singleton: Optional[_BTCDCalibrator] = None


def get_btcd_calibrator() -> _BTCDCalibrator:
    global _singleton
    if _singleton is None:
        _singleton = _BTCDCalibrator()
    return _singleton
