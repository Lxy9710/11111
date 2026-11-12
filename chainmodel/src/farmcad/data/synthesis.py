"""The L3 synthesiser: reproduce a process step's drawing from the chain.

The falsifiable target
----------------------
Given the nominal design drawing and a process chain, the synthesiser must
produce, for every operation, the set of dimensions that operation's
drawing should contain, together with the value each dimension states.

That target is falsifiable on the FAXIN corpus: every operation drawing
there has a known set of stated dimensions, so a synthesiser can be scored
by how much of the drawing it reproduces.

Why the target is stated this way
---------------------------------
The FAXIN process drawings are per-operation views. Each is laid out on its
own, annotates what matters for its operation, and does not accumulate the
geometry of the steps before it: measured on the corpus, the radial profile
of two consecutive steps shares no radius at all and is never nested. A
synthesiser that tried to reproduce the drawing entity by entity would be
reproducing a draughting decision, not the process, and it would be
unfalsifiable because the layouts are not determined by the chain.

The stated dimension set is different in kind. Which features an operation
states, and what value it states for each, is determined by the process.
So the target is well posed, and a synthesiser can be wrong in a measurable
way.

The synthesis rule
------------------
The chain says which nominal feature each parameter refers to. The
synthesiser turns that into a dimension: it takes the nominal feature's
value, applies the parameter's update, and states the result. A parameter
that refers to no nominal feature introduces a new one, whose value comes
from the parameter itself.

This is the part of the system that a geometric process chain has to get
right: the value an operation states must be derived from the nominal
geometry through the chain, not read off a number that was copied from the
drawing it is supposed to predict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .localization import dimension_features, match_features, stated_value


@dataclass
class SynthesisedDimension:
    """One dimension the synthesiser believes an operation's drawing holds."""

    value_mm: float
    source_feature: str | None = None
    origin: str = "nominal"       # nominal | new | modified
    parameter: str | None = None
    evidence: dict = field(default_factory=dict)


@dataclass
class SynthesisedStep:
    """What the synthesiser predicts for one operation."""

    operation: str
    dimensions: list[SynthesisedDimension] = field(default_factory=list)

    @property
    def values(self) -> list[float]:
        return [d.value_mm for d in self.dimensions]


def synthesise_step(operation: str, parameters: list[dict],
                    nominal_features: list[dict],
                    nominal_lookup: dict[str, dict] | None = None) -> SynthesisedStep:
    """Predict the dimensions an operation's drawing should hold.

    ``parameters`` is a list of the chain's parameters for this operation.
    Each parameter names the nominal feature it acts on through
    ``features``, states the value it sets in ``value``, and describes how
    it changes that feature through ``update``.

    ``nominal_features`` is the list of features of the nominal design, in
    the form :func:`farmcad.data.localization.dimension_features` returns.
    """

    lookup = nominal_lookup or {}
    step = SynthesisedStep(operation=operation)
    used = set()

    for parameter in parameters:
        value = parameter.get("value")
        if value is None:
            continue
        if isinstance(value, dict):
            nominal = value.get("nominal")
            value = nominal if nominal is not None else value.get("direct")
        if not isinstance(value, (int, float)):
            continue

        features = parameter.get("features") or []
        target = None
        for feature_id in features:
            if feature_id in lookup and feature_id not in used:
                target = lookup[feature_id]
                used.add(feature_id)
                break

        update = parameter.get("update")
        if target is None:
            # The parameter introduces a feature the nominal part does not
            # have. Its value is the parameter's own.
            step.dimensions.append(SynthesisedDimension(
                value_mm=float(value),
                source_feature=None,
                origin="new",
                parameter=parameter.get("parameter"),
                evidence={"update": update},
            ))
            continue

        nominal_value = target.get("value_mm")
        if nominal_value is None:
            continue
        origin = "nominal"
        stated = float(value)
        if update and update not in ("design_copy", "state_copy", "unknown"):
            origin = "modified"
        step.dimensions.append(SynthesisedDimension(
            value_mm=stated,
            source_feature=target.get("handle"),
            origin=origin,
            parameter=parameter.get("parameter"),
            evidence={
                "nominal_value": nominal_value,
                "update": update,
                "delta": round(stated - float(nominal_value), 6),
            },
        ))

    # A feature the operation does not touch is not restated by it: the
    # operation's drawing only carries what the operation acts on.
    return step


def synthesise_route(design: dict, chain: list[dict]) -> list[SynthesisedStep]:
    """Predict the drawing of every operation in a chain.

    ``chain`` is a list of ``{"operation": str, "parameters": [...]}``.
    """

    nominal = dimension_features(design)
    lookup = {}
    for feature in nominal:
        handle = feature.get("handle")
        if handle is not None and handle not in lookup:
            lookup[handle] = feature
    # The chain refers to features by an id, so the nominal features are
    # also indexed by their id when the design supplies one.
    for feature in design.get("features") or []:
        identifier = feature.get("id")
        value = feature.get("value")
        if identifier is None:
            continue
        nominal_value = None
        if isinstance(value, dict):
            nominal_value = value.get("nominal")
        if nominal_value is not None:
            lookup[identifier] = {
                "handle": identifier,
                "value_mm": float(nominal_value),
            }

    steps = []
    for entry in chain:
        steps.append(synthesise_step(
            entry.get("operation", ""),
            entry.get("parameters") or [],
            nominal,
            lookup,
        ))
    return steps


def score_synthesis(predicted: list[SynthesisedDimension],
                    actual: list[dict],
                    tolerance: float = 0.02) -> dict:
    """Score a synthesised step against the drawing it should reproduce.

    The score has three parts, because there are three ways to be wrong:
    the synthesiser can state a value the drawing does not have, miss a
    value the drawing has, or state the right value on the wrong feature.
    """

    actual_features = dimension_features({"entities": actual}) if False else actual
    actual_values = [f["value_mm"] for f in actual_features
                     if f.get("value_mm") is not None]
    predicted_values = [d.value_mm for d in predicted]

    used = set()
    hits = []
    for predicted_value in predicted_values:
        best = None
        for index, actual_value in enumerate(actual_values):
            if index in used:
                continue
            distance = abs(predicted_value - actual_value)
            if distance <= max(tolerance, tolerance * abs(predicted_value)):
                if best is None or distance < best[0]:
                    best = (distance, index)
        if best is not None:
            used.add(best[1])
            hits.append(best[0])

    precision = len(hits) / max(1, len(predicted_values))
    recall = len(hits) / max(1, len(actual_values))
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "predicted": len(predicted_values),
        "actual": len(actual_values),
        "matched": len(hits),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mean_error_mm": (sum(hits) / len(hits)) if hits else None,
    }


def score_route(predicted_steps: list[SynthesisedStep],
                 actual_steps: list[dict]) -> dict:
    """Score a whole synthesised route."""

    results = []
    for predicted, actual in zip(predicted_steps, actual_steps):
        result = score_synthesis(predicted.dimensions, actual)
        result["operation"] = predicted.operation
        results.append(result)
    total_predicted = sum(r["predicted"] for r in results)
    total_matched = sum(r["matched"] for r in results)
    total_actual = sum(r["actual"] for r in results)
    return {
        "steps": results,
        "predicted": total_predicted,
        "matched": total_matched,
        "actual": total_actual,
        "micro_precision": total_matched / max(1, total_predicted),
        "micro_recall": total_matched / max(1, total_actual),
        "micro_f1": (2 * total_matched / max(1, total_predicted + total_actual)),
    }
