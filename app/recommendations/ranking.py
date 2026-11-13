"""Score explicit features; this module performs no retrieval, I/O or LLM calls."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
import math
from numbers import Real
from types import MappingProxyType


DEFAULT_WEIGHTS = MappingProxyType({"grade_a_rate": 0.6, "professor_rating": 0.4})
DEFAULT_RELIABILITY = MappingProxyType({"grade_a_rate": 100.0, "professor_rating": 10.0})
_FEATURES = tuple(DEFAULT_WEIGHTS)
_SCALES = {"grade_a_rate": (0.0, 1.0), "professor_rating": (1.0, 5.0)}


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _config(values, defaults, *, weights: bool) -> dict[str, float]:
    if values is None:
        result = dict(defaults)
    else:
        if not isinstance(values, Mapping) or set(values) - set(_FEATURES):
            raise ValueError("configuration must map supported feature names to finite numbers")
        result = {name: 0.0 for name in _FEATURES} if weights else dict(defaults)
        for name, value in values.items():
            numeric = _number(value)
            if numeric is None or numeric < 0 or (not weights and numeric == 0):
                raise ValueError("weights must be finite and nonnegative; reliability constants must be finite and positive")
            result[name] = numeric
    if weights:
        largest = max(result.values())
        if largest <= 0:
            raise ValueError("at least one weight must be positive")
        scaled = {name: value / largest for name, value in result.items()}
        total = math.fsum(scaled.values())
        result = {name: value / total for name, value in scaled.items()}
    return result


def _score_feature(name: str, entry, k: float) -> dict:
    absent = entry is None
    shape_valid = isinstance(entry, Mapping)
    entry = entry if shape_valid else {}
    raw = entry.get("value")
    sample = entry.get("sample_count")
    value, n = _number(raw), _number(sample)
    reason = entry.get("missing_reason") or None
    if not reason:
        if not shape_valid:
            reason = "feature_missing" if absent else "invalid_feature_shape"
        elif raw is None:
            reason = "missing_value"
        elif value is None:
            reason = "invalid_value"
        elif not _SCALES[name][0] <= value <= _SCALES[name][1]:
            reason = "value_out_of_range"
        elif sample is None:
            reason = "missing_sample_count"
        elif n is None:
            reason = "invalid_sample_count"
        elif n <= 0:
            reason = "nonpositive_sample_count"

    normalized = adjusted = shrinkage = None
    if shape_valid and value is not None and _SCALES[name][0] <= value <= _SCALES[name][1] and not entry.get("missing_reason"):
        normalized = value * 100.0 if name == "grade_a_rate" else (value - 1.0) * 25.0
    if not reason:
        denominator = n + k
        shrinkage = n / denominator if math.isfinite(denominator) else 1.0 / (1.0 + k / n)
        adjusted = 50.0 + shrinkage * (normalized - 50.0)
    uncertainty = reason or entry.get("uncertain_reason") or None
    return {
        **deepcopy(dict(entry)),
        "raw": deepcopy(raw),
        "normalized": normalized,
        "adjusted": adjusted,
        "sample_count": deepcopy(sample),
        "reliability_k": k,
        "reliability_factor": shrinkage,
        "valid": reason is None,
        "missing_reason": deepcopy(reason),
        "uncertain": uncertainty is not None,
        "uncertain_reason": deepcopy(uncertainty),
        "effective_weight": 0.0,
        "contribution": None,
    }


def rank_candidates(
    candidates: Iterable[Mapping],
    weights: Mapping | None = None,
    reliability: Mapping | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Rank all supplied candidates, then slice TopK; preserve payloads and ties.

    ``candidate['features'][name]`` is ``{value, sample_count, ...metadata}``.
    A rates use 0–1; professor ratings use 1–5. Scores use 0–100 and sample
    shrinkage toward 50; they are decision scores, not statistical probabilities.
    Explicit weights omit a feature by giving it weight zero. Missing features
    redistribute weights among valid weighted features; no usable features
    yield ``final_score=None`` and sort last. Partial reliability overrides
    default k values. This function never filters or retrieves candidates.
    """
    configured = _config(weights, DEFAULT_WEIGHTS, weights=True)
    constants = _config(reliability, DEFAULT_RELIABILITY, weights=False)
    if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0):
        raise ValueError("top_k must be a nonnegative integer or None")
    ranked = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ValueError("each candidate must be a mapping")
        payload = deepcopy(dict(candidate))
        entries = candidate.get("features")
        entries = entries if isinstance(entries, Mapping) else {}
        features = {name: _score_feature(name, entries.get(name), constants[name]) for name in _FEATURES}
        usable_weight = math.fsum(configured[name] for name in _FEATURES if features[name]["valid"])
        effective = {
            name: configured[name] / usable_weight if features[name]["valid"] and usable_weight else 0.0
            for name in _FEATURES
        }
        for name, feature in features.items():
            feature["effective_weight"] = effective[name]
            if feature["valid"]:
                feature["contribution"] = effective[name] * feature["adjusted"]
        score = math.fsum(feature["contribution"] or 0.0 for feature in features.values()) if usable_weight else None
        payload["ranking"] = {
            "score_version": "sample_adjusted_weighted_v1",
            "configured_weights": dict(configured),
            "effective_weights": effective,
            "features": features,
            "missing_features": [name for name in _FEATURES if not features[name]["valid"]],
            "uncertain_features": [name for name in _FEATURES if features[name]["uncertain"]],
            "final_score": score,
            "score_reason": None if score is not None else "no_usable_weighted_features",
        }
        ranked.append(payload)
    ranked.sort(key=lambda row: (row["ranking"]["final_score"] is None, -(row["ranking"]["final_score"] or 0.0)))
    return ranked if top_k is None else ranked[:top_k]
