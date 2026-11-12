from __future__ import annotations

from farmcad.data.dataset import Case, Vocabulary
from farmcad.data.schema import VERSION, label
from farmcad.models.model import FARMModel, ModelConfig


def example(case_id: str = "example") -> tuple[dict, dict]:
    design = {
        "schema_version": VERSION, "case_id": case_id,
        "entities": [
            {"id": "L", "type": "LINE", "numeric": [0.0] * 12, "text": ""},
            {"id": "A", "type": "DIMENSION", "numeric": [0.1] * 12, "text": "10 +/-0.1"},
        ],
        "edges": [[0, 0, 0], [0, 1, 1], [1, 0, 1], [1, 1, 0]],
        "features": [{"id": "diameter", "text": "diameter", "anchor_entities": ["A"],
                      "value": {"nominal": 10.0, "lower": -0.1, "upper": 0.1, "direct": None, "unit": "mm"}}],
        "requirements": [{"id": "diameter:r0", "feature_id": "diameter", "text": "dimension range",
                          "lower": 9.9, "upper": 10.1, "evidence_entities": ["A"]}],
        "context": "steel outer ring",
    }
    events = []
    for index, name in enumerate(("grind", "inspect")):
        events.append({"id": f"op{index}", "action": "OP", "operation": name,
                       "parameters_complete": True, "quality": "gold"})
        events.append({
            "id": f"s{index}", "action": "PARAM", "parameter": "D", "quality": "gold",
            "labels": {
                "features": label(["diameter"], "gold"),
                "requirements": label(["diameter:r0"], "gold"),
                "sources": label([] if index == 0 else ["s0"], "gold"),
                "roles": label(["machining_target"] if index == 0 else ["inspection_target"], "gold"),
                "update": label("design_copy" if index == 0 else "state_copy", "gold"),
                "value": label(dict(design["features"][0]["value"]), "gold"),
            },
        })
        events.append({"id": f"end{index}", "action": "END_OP", "quality": "gold"})
    events.append({"id": "eos", "action": "EOS", "quality": "gold"})
    target = {"schema_version": VERSION, "case_id": case_id, "reviewer": "synthetic-test-only",
              "events": events, "feature_members": {"diameter": label(["L"], "gold")}}
    return design, target


def tiny_model(design: dict, target: dict) -> FARMModel:
    vocabulary = Vocabulary.build([Case(design["case_id"], "family", "train", design, target)])
    return FARMModel(ModelConfig(hidden=16, graph_layers=1, dropout=0.0, text_bytes=32,
                                max_events=24, max_operations=4, max_parameters=3), vocabulary)
