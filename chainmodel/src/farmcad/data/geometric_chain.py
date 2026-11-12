"""The geometric chain: carrying the geometry, not inferring it.

The L3 model tried to *infer* how much an operation takes off a feature,
from the kind of operation and the size of the feature. Measured on the
corpus, that does not work: 67.5 per cent of the changes are exactly zero,
and the mean change within every group of (operation kind, feature size)
is indistinguishable from zero. The material removed from a feature is
decided by the process plan, not by the kind of operation.

So the value has to be *carried*, not inferred. That is exactly what a
geometric process chain is for: it states, for every operation, which
nominal feature is acted on and what it becomes.

This module builds that chain and synthesises the drawing from it.

The chain
---------
For every operation, the chain records the features it acts on:

    touched     the nominal feature, the value it becomes, and the delta
    introduced  the features it creates that the nominal part does not have

The chain is what makes a cascaded modification checkable. A parameter
chain says "the outer diameter becomes 65.9"; a geometric chain says "the
outer diameter feature, which is the 65.9 wide one on the nominal part,
becomes 65.9, and the 6.0 wide one becomes 6.7", so the two can be
compared, and a step that contradicts an earlier one can be found.

The synthesis
-------------
Reading the chain back gives the operation's drawing. The score is then
exact for every feature the chain carries, which is the point: the chain
is sufficient. What it does not contain, it cannot reproduce, and the gap
between the chain and the drawing is the coverage the chain still lacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .localization import dimension_features, localise_onto_design


@dataclass
class ChainEntry:
    """One feature an operation acts on."""

    nominal_handle: str
    nominal_value_mm: float
    stated_value_mm: float
    introduced: bool = False

    @property
    def delta_mm(self) -> float:
        return self.stated_value_mm - self.nominal_value_mm


@dataclass
class ChainOperation:
    """The geometric chain of one operation."""

    operation: str
    entries: list[ChainEntry] = field(default_factory=list)

    @property
    def values(self) -> list[float]:
        return [e.stated_value_mm for e in self.entries]


@dataclass
class GeometricChain:
    """The geometric counterpart of a parameter chain."""

    case_id: str
    operations: list[ChainOperation] = field(default_factory=list)

    def synthesise(self) -> list[list[float]]:
        """The dimension values each operation's drawing should hold."""

        return [operation.values for operation in self.operations]


def build_chain(design: dict, steps: list[dict], case_id: str = "") -> GeometricChain:
    """Build the geometric chain of a part from its operation drawings.

    ``steps`` is a list of ``{"label": str, "data": dict}``.
    """

    chain = GeometricChain(case_id=case_id)
    for step in steps:
        operation = ChainOperation(operation=step["label"])
        localisation = localise_onto_design(design, step["data"])
        for match in localisation["matches"]:
            operation.entries.append(ChainEntry(
                nominal_handle=match["target_handle"],
                nominal_value_mm=match["target_value_mm"],
                stated_value_mm=match["source_value_mm"],
            ))
        for feature in localisation["unmatched_operation"]:
            operation.entries.append(ChainEntry(
                nominal_handle=feature["handle"],
                nominal_value_mm=feature["value_mm"],
                stated_value_mm=feature["value_mm"],
                introduced=True,
            ))
        chain.operations.append(operation)
    return chain


def score_chain(chain: GeometricChain, steps: list[dict],
                tolerance: float = 0.02) -> dict:
    """Score the chain against the drawings it should reproduce."""

    predicted_total = matched_total = actual_total = 0
    per_operation = []
    for operation, step in zip(chain.operations, steps):
        actual = dimension_features(step["data"])
        actual_values = [f["value_mm"] for f in actual]
        predicted_values = operation.values

        used = set()
        hits = 0
        for value in predicted_values:
            best = None
            for index, candidate in enumerate(actual_values):
                if index in used:
                    continue
                distance = abs(value - candidate)
                if distance <= max(tolerance, tolerance * abs(value)):
                    if best is None or distance < best[0]:
                        best = (distance, index)
            if best is not None:
                used.add(best[1])
                hits += 1

        predicted_total += len(predicted_values)
        matched_total += hits
        actual_total += len(actual_values)
        per_operation.append({
            "operation": operation.operation,
            "predicted": len(predicted_values),
            "matched": hits,
            "actual": len(actual_values),
            "introduced": sum(1 for e in operation.entries if e.introduced),
        })

    return {
        "predicted": predicted_total,
        "matched": matched_total,
        "actual": actual_total,
        "precision": matched_total / max(1, predicted_total),
        "recall": matched_total / max(1, actual_total),
        "f1": 2 * matched_total / max(1, predicted_total + actual_total),
        "per_operation": per_operation,
    }


def chain_coverage(chain: GeometricChain, steps: list[dict]) -> dict:
    """How much of each drawing the chain accounts for.

    The chain can only state what it carries. This measures the part of a
    drawing that the chain leaves unexplained, which is the part a richer
    chain would have to cover.
    """

    covered = total = 0
    for operation, step in zip(chain.operations, steps):
        actual = dimension_features(step["data"])
        stated = operation.values
        for feature in actual:
            total += 1
            value = feature["value_mm"]
            if any(abs(value - s) <= max(0.02, 0.02 * abs(value)) for s in stated):
                covered += 1
    return {
        "covered": covered,
        "total": total,
        "coverage": covered / max(1, total),
    }
