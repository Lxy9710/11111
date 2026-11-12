from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def scaled(value: float) -> float:
    return math.copysign(math.log1p(abs(value)), value)


def unscaled(value: float) -> float:
    return math.copysign(math.expm1(min(abs(value), 20.0)), value)


def require_keys(data: dict, allowed: set, required: set, location: str) -> None:
    extra, missing = set(data) - allowed, required - set(data)
    if extra or missing:
        raise ValueError(f"{location}: unexpected={sorted(extra)}, missing={sorted(missing)}")
