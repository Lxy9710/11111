from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from farmcad.data.schema import VERSION, validate_design, validate_target
from farmcad.utils import object_hash, read_json, require_keys


@dataclass
class Case:
    case_id: str
    family_id: str
    split: str
    design: dict
    target: dict


def load_cases(manifest_path: Path, smoke: bool = False) -> list[Case]:
    manifest = read_json(manifest_path)
    require_keys(manifest, {"schema_version", "cases"}, {"schema_version", "cases"}, "manifest")
    if manifest["schema_version"] != VERSION:
        raise ValueError("Unsupported manifest schema")
    cases = []
    identities, families, drawings = set(), {}, {}
    for row in manifest["cases"]:
        keys = {"case_id", "family_id", "split", "design", "target"}
        require_keys(row, keys, keys, "manifest case")
        case_id, family, split = row["case_id"], row["family_id"], row["split"]
        if case_id in identities or not family.strip() or split not in ("train", "validation", "test"):
            raise ValueError("Duplicate case, missing family, or invalid split")
        identities.add(case_id)
        if family in families and families[family] != split:
            raise ValueError(f"Family leakage across splits: {family}")
        families[family] = split
        design = read_json(manifest_path.parent / row["design"])
        target = read_json(manifest_path.parent / row["target"])
        validate_design(design)
        validate_target(design, target)
        if design["case_id"] != case_id:
            raise ValueError("Manifest/design identity mismatch")
        fingerprint = object_hash({k: v for k, v in design.items() if k != "case_id"})
        if fingerprint in drawings and drawings[fingerprint] != split:
            raise ValueError("Identical design leaked across splits")
        drawings[fingerprint] = split
        cases.append(Case(case_id, family, split, design, target))
    if not cases or not any(c.split == "train" for c in cases):
        raise ValueError("Dataset requires at least one training case")
    if not smoke and not any(c.split == "validation" for c in cases):
        raise ValueError("Independent validation family required; --smoke is only for pipeline checks")
    return cases


class Vocabulary:
    def __init__(self, operations: list[str], parameters: list[str]):
        self.operations = operations
        self.parameters = parameters
        for tokens in (operations, parameters):
            if tokens[:2] != ["<PAD>", "<UNK>"] or len(set(tokens)) != len(tokens):
                raise ValueError("Invalid vocabulary")
        self.op_index = {token: i for i, token in enumerate(operations)}
        self.param_index = {token: i for i, token in enumerate(parameters)}

    @classmethod
    def build(cls, cases: list[Case]) -> Vocabulary:
        training = [c for c in cases if c.split == "train"]
        ops = sorted({e["operation"] for c in training for e in c.target["events"] if e["action"] == "OP"})
        params = sorted({e["parameter"] for c in training for e in c.target["events"] if e["action"] == "PARAM"})
        if not ops:
            raise ValueError("No training operation vocabulary")
        return cls(["<PAD>", "<UNK>"] + ops, ["<PAD>", "<UNK>"] + params)

    def to_dict(self) -> dict:
        return {"operations": self.operations, "parameters": self.parameters}

    def coverage(self, cases: list[Case]) -> dict:
        result = {}
        for split in ("train", "validation", "test"):
            events = [e for c in cases if c.split == split for e in c.target["events"]]
            result[split] = {
                "unknown_operations": sum(e["action"] == "OP" and e["operation"] not in self.op_index for e in events),
                "unknown_parameters": sum(e["action"] == "PARAM" and e["parameter"] not in self.param_index for e in events),
            }
        return result
