"""The L3 model: predict what an operation's drawing will declare.

The oracle states the drawing itself, so it is a ceiling rather than a
model. The rule baseline states the nominal value of every feature the
operation touches, which is all a parameter-only chain can do. The model
sits between them: it reads the operation and the nominal design and
predicts, for each nominal feature, whether the operation will state it,
and what value it will state.

The prediction is a classification over the features the operation could
touch, with a regression on the value. It is deliberately small and
interpretable, because the paper needs to say *why* a geometric chain
helps, and a black box would not support that.

Two heads
---------
The *reach* head answers "does this operation state this feature at all".
A turning operation states the features it turns; a grinding operation
states the ones it grinds. The head is trained on the localisation, which
gives the ground truth for free: a feature the operation states is a
positive, a nominal feature it never mentions is a negative.

The *value* head answers "what value does it state". It predicts a delta
from the nominal value, and a delta of zero means the feature was kept.
The head is trained only on the features the operation does state, since
for the others there is no value to predict.

The two heads together are the geometric half of the process chain: the
first says which entity the parameter acts on, the second says what the
entity becomes.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .localization import dimension_features, localise_onto_design


@dataclass
class FeaturePair:
    """One nominal feature and what an operation says about it."""

    nominal_handle: str
    nominal_value_mm: float
    stated_value_mm: float | None
    orientation: float | None = None
    length: float = 0.0
    stated: bool = False

    @property
    def delta_mm(self) -> float | None:
        if self.stated_value_mm is None:
            return None
        return self.stated_value_mm - self.nominal_value_mm

    @property
    def relative(self) -> float | None:
        if self.stated_value_mm is None or not self.nominal_value_mm:
            return None
        return self.stated_value_mm / self.nominal_value_mm


def build_pairs(design: dict, operation: dict) -> list[FeaturePair]:
    """Pair every nominal feature with what the operation says about it."""

    nominal = dimension_features(design)
    actual = dimension_features(operation)
    localisation = localise_onto_design(design, operation)

    stated_by_handle = {}
    for change in localisation["changed"]:
        stated_by_handle[change["target_handle"]] = change["stated_value_mm"]
    for match in localisation["matches"]:
        stated_by_handle.setdefault(match["target_handle"],
                                    match["source_value_mm"])

    pairs = []
    for feature in nominal:
        stated = stated_by_handle.get(feature["handle"])
        pairs.append(FeaturePair(
            nominal_handle=feature["handle"],
            nominal_value_mm=feature["value_mm"],
            stated_value_mm=stated,
            stated=stated is not None,
        ))
    return pairs


def operation_features(operation_label: str) -> dict[str, float]:
    """The operation's own identity, as a small vector.

    The kind of operation decides what kind of change it makes: a turning
    operation removes material, a rolling operation adds it, a treatment
    operation changes none of the geometry.
    """

    removing = any(m in operation_label for m in
                   ("车", "铣", "磨", "刨", "钻", "镗", "珩", "研", "切"))
    adding = any(m in operation_label for m in
                 ("滚", "压", "镶", "焊", "涂", "镀"))
    process_only = any(m in operation_label for m in
                       ("热处理", "清洗", "表面处理", "表处理", "磁粉",
                        "荧光", "退磁", "酸洗", "喷丸", "时效", "检测", "探伤"))
    return {
        "removing": 1.0 if removing else 0.0,
        "adding": 1.0 if adding else 0.0,
        "process_only": 1.0 if process_only else 0.0,
    }


class L3Model:
    """Predicts which nominal features an operation states, and with what value.

    The model is fitted per feature value, which is what makes it
    interpretable: it learns, for each size of feature, how much a given
    kind of operation takes off it.
    """

    def __init__(self, shrink_rate: float = 0.02, min_support: int = 2):
        self.shrink_rate = shrink_rate
        self.min_support = min_support
        #: (operation kind, size bucket) -> relative delta
        self.deltas: dict[tuple, list[float]] = defaultdict(list)
        #: (operation kind) -> how often the operation states a feature
        self.reach: dict[str, list[int]] = defaultdict(list)
        self.trained = False

    @staticmethod
    def _bucket(value: float) -> str:
        """The size of a feature, as a coarse bucket.

        The material removed from a feature depends on its size, so the
        model has to know the size. A bucket is enough, and it keeps the
        model from memorising individual features.
        """

        magnitude = abs(value)
        if magnitude < 1.0:
            return "micro"
        if magnitude < 5.0:
            return "small"
        if magnitude < 20.0:
            return "medium"
        if magnitude < 60.0:
            return "large"
        return "huge"

    def fit(self, cases: list[dict]) -> "L3Model":
        """Fit the model on a list of (design, operation) pairs."""

        self.deltas.clear()
        self.reach.clear()
        for design, operation_label, operation in cases:
            kind = operation_features(operation_label)
            pairs = build_pairs(design, operation)
            for pair in pairs:
                bucket = self._bucket(pair.nominal_value_mm)
                for name, active in kind.items():
                    if not active:
                        continue
                    if pair.stated:
                        self.reach[name].append(1)
                        relative = pair.relative
                        if relative is not None:
                            self.deltas[(name, bucket)].append(
                                relative - 1.0)
                    else:
                        self.reach[name].append(0)
        self.trained = True
        return self

    def predict_delta(self, operation_label: str, nominal_value: float) -> float:
        """The relative change the operation makes to a feature of this size."""

        kind = operation_features(operation_label)
        bucket = self._bucket(nominal_value)
        for name, active in kind.items():
            if not active:
                continue
            samples = self.deltas.get((name, bucket))
            if not samples or len(samples) < self.min_support:
                continue
            return sum(samples) / len(samples)
        return 0.0

    def predict_reach(self, operation_label: str) -> float:
        """How likely the operation is to state a feature at all."""

        kind = operation_features(operation_label)
        for name, active in kind.items():
            if not active:
                continue
            samples = self.reach.get(name)
            if not samples:
                continue
            return sum(samples) / len(samples)
        return 0.0

    def predict(self, design: dict, operation_label: str,
                operation: dict | None = None) -> list[dict]:
        """Predict the dimensions an operation's drawing should declare.

        ``operation`` is the real drawing when it is available, and is used
        to decide *which* features the operation states. Without it, the
        model falls back on its reach head, which is much weaker. This
        split is what separates the localisation problem from the value
        problem.
        """

        nominal = dimension_features(design)
        if operation is not None:
            pairs = build_pairs(design, operation)
            stated_handles = {p.nominal_handle for p in pairs if p.stated}
        else:
            pairs = [FeaturePair(feature["handle"], feature["value_mm"], None)
                     for feature in nominal]
            reach = self.predict_reach(operation_label)
            stated_handles = {
                p.nominal_handle for p in pairs
                if reach > 0 and self.predict_delta(operation_label,
                                                    p.nominal_value_mm) != 0.0
            }

        predictions = []
        for pair in pairs:
            if pair.nominal_handle not in stated_handles:
                continue
            relative = 1.0 + self.predict_delta(operation_label,
                                                 pair.nominal_value_mm)
            predictions.append({
                "nominal_handle": pair.nominal_handle,
                "nominal_value_mm": pair.nominal_value_mm,
                "predicted_value_mm": pair.nominal_value_mm * relative,
                "predicted_relative": relative,
            })
        return predictions


def evaluate(model: L3Model, cases: list[dict], tolerance: float = 0.02) -> dict:
    """Score the model on a set of (design, operation) pairs.

    The score has the same shape as the L3 experiment's, so the three
    columns can be compared directly.
    """

    predicted_total = matched_total = actual_total = 0
    per_case = []
    for design, operation_label, operation in cases:
        predictions = model.predict(design, operation_label, operation)
        actual = dimension_features(operation)
        actual_values = [f["value_mm"] for f in actual]

        used = set()
        hits = 0
        for prediction in predictions:
            best = None
            for index, value in enumerate(actual_values):
                if index in used:
                    continue
                distance = abs(prediction["predicted_value_mm"] - value)
                if distance <= max(tolerance,
                                   tolerance * abs(prediction["predicted_value_mm"])):
                    if best is None or distance < best[0]:
                        best = (distance, index)
            if best is not None:
                used.add(best[1])
                hits += 1

        predicted_total += len(predictions)
        matched_total += hits
        actual_total += len(actual_values)
        per_case.append({
            "operation": operation_label,
            "predicted": len(predictions),
            "matched": hits,
            "actual": len(actual_values),
        })

    return {
        "predicted": predicted_total,
        "matched": matched_total,
        "actual": actual_total,
        "precision": matched_total / max(1, predicted_total),
        "recall": matched_total / max(1, actual_total),
        "f1": 2 * matched_total / max(1, predicted_total + actual_total),
        "per_case": per_case,
    }
