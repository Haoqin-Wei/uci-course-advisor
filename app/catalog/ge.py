"""Normalize GE storage syntax independently of evidence or student credit."""
from __future__ import annotations

import json
import re

GE_CODES = frozenset({"I", "IA", "IB", "II", "III", "IV", "V", "VA", "VB", "VI", "VII", "VIII"})


def normalize_ge_category(value):
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    match = re.fullmatch(r"(?:GE\s*[-_:]?\s*)?([IV]+[AB]?)(?:\s*:\s*[^|]+)?", text)
    return match[1] if match and match[1] in GE_CODES else None


def parse_ge_categories(value):
    """Accept existing pipe/single labels and JSON lists without asserting trust."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                value = [text]
        else:
            value = text.split("|") if text else []
    if not isinstance(value, (tuple, list)):
        return {"categories": [], "unrecognized": [value] if value is not None else []}
    labels, unknown = [], []
    for raw in value:
        label = normalize_ge_category(raw)
        if label is None:
            if raw not in (None, ""):
                unknown.append(raw)
        elif label not in labels:
            labels.append(label)
    return {"categories": labels, "unrecognized": unknown}
