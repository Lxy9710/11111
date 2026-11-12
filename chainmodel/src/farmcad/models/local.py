from __future__ import annotations

import torch
from torch import Tensor, nn

from farmcad.data.dataset import Vocabulary
from farmcad.data.schema import ROLES, UPDATES, validate_design, validate_target
from farmcad.memory.addressing import AddressResolver
from farmcad.models.encoder import DesignEncoder, DesignMemory, value_vector
from farmcad.models.model import ModelConfig, StepResult, unknown_value


class LocalPredictor(nn.Module):
    """Design perception and operation-conditioned local predictions, without a route decoder."""

    def __init__(self, config: ModelConfig, vocabulary: Vocabulary):
        super().__init__()
        self.config, self.vocabulary = config, vocabulary
        h = config.hidden
        self.encoder = DesignEncoder(h, config.graph_layers, config.dropout, config.text_bytes)
        self.operation_embedding = nn.Embedding(len(vocabulary.operations), h)
        self.parameter_embedding = nn.Embedding(len(vocabulary.parameters), h)
        self.query = nn.Sequential(nn.Linear(h * 3, h), nn.GELU(), nn.LayerNorm(h))
        self.parameter_head = nn.Linear(h, len(vocabulary.parameters))
        self.addressing = AddressResolver(h, config.pointer_slots)
        self.role_head = nn.Linear(h, len(ROLES))
        self.update_head = nn.Linear(h, len(UPDATES))
        self.value_head = nn.Linear(h, 4)
        self.value_mask_head = nn.Linear(h, 4)
        self.unit_head = nn.Linear(h, 2)
        self.value_embedding = nn.Linear(8, h)
        self.unit_embedding = nn.Embedding(3, h)
        self.role_embedding = nn.Linear(len(ROLES), h)
        self.update_embedding = nn.Embedding(len(UPDATES), h)
        self.record_encoder = nn.Sequential(nn.Linear(h * 3, h), nn.GELU(), nn.LayerNorm(h))
        # 已发出参数的序列编码：使 parameter 头能看到"本工序已产出哪些参数"，
        # 否则 logits 在同一工序内恒定，参数头被锁死在一个固定上界。
        self.emitted_encoder = nn.Sequential(
            nn.Linear(h * 2, h), nn.GELU(), nn.LayerNorm(h))
        self.emitted_projection = nn.Linear(h, h)
        self.start_token = nn.Parameter(torch.zeros(h))

    def encode(self, design: dict) -> DesignMemory:
        validate_design(design)
        if len(design["entities"]) > self.config.max_entities:
            raise ValueError("Design exceeds max_entities")
        return self.encoder(design)

    def record_vectors(self, design: DesignMemory, records: list[dict]) -> Tensor:
        vectors = []
        for record in records:
            labels = record["labels"]
            features = self.addressing.features.pool(
                labels["features"]["value"] or ["UNKNOWN"], design.feature_ids, design.features)
            requirements = self.addressing.requirements.pool(
                labels["requirements"]["value"] or ["UNKNOWN"], design.requirement_ids, design.requirements)
            value = labels["value"]["value"] or unknown_value()
            attrs = (
                self.operation_embedding.weight[self.vocabulary.op_index.get(record["operation"], 1)]
                + self.parameter_embedding.weight[self.vocabulary.param_index.get(record["parameter"], 1)]
                + self.value_embedding(design.context.new_tensor(value_vector(value)))
                + self.unit_embedding.weight[("unknown", "mm", "um").index(value["unit"])]
                + self.role_embedding(design.context.new_tensor(
                    [float(r in (labels["roles"]["value"] or [])) for r in ROLES]))
                + self.update_embedding.weight[UPDATES.index(labels["update"]["value"] or "unknown")]
            )
            vectors.append(self.record_encoder(torch.cat([features, requirements, attrs])))
        return torch.stack(vectors) if vectors else design.context.new_zeros((0, self.config.hidden))

    def emitted_state(self, design: DesignMemory, records: list[dict], operation_index: int) -> Tensor:
        """编码"本工序已发出的参数"序列。

        不含当前工序之外的记录，因此与 operation_index 无关；
        空序列用一个可学习的 start token 表示，使 query 始终定长。
        """
        h = self.config.hidden
        emitted = [r for r in records if r.get("operation_index") == operation_index]
        if not emitted:
            return self.start_token.expand(1, h).clone()
        tokens = torch.stack([
            self.parameter_embedding.weight[self.vocabulary.param_index.get(r["parameter"], 1)]
            for r in emitted
        ])
        order = torch.arange(len(emitted), device=tokens.device, dtype=torch.float32).unsqueeze(1)
        # 位置编码，让模型能区分"已发出的第几个参数"
        pos = order / max(len(emitted), 1)
        projected = self.emitted_projection(tokens)
        return self.emitted_encoder(torch.cat([projected, projected * pos], dim=-1)).mean(dim=0, keepdim=True)

    def step(self, design: DesignMemory, operation: str, parameter: str,
             records: list[dict], operation_index: int) -> StepResult:
        op = self.operation_embedding.weight[self.vocabulary.op_index.get(operation, 1)]
        state = self.emitted_state(design, records, operation_index)
        hidden = self.query(torch.cat([design.context, op, state.squeeze(0)]))
        parameter_logits = self.parameter_head(hidden)
        query = hidden + self.parameter_embedding.weight[self.vocabulary.param_index.get(parameter, 1)]
        vectors = self.record_vectors(design, records)
        valid = [r["operation_index"] < operation_index for r in records]
        heads = self.addressing(query, design.features, design.requirements, vectors, valid)
        heads.update(parameter=parameter_logits, roles=self.role_head(query), update=self.update_head(query),
                     value=self.value_head(query), value_mask=self.value_mask_head(query), unit=self.unit_head(query))
        return StepResult(heads, hidden, [r["id"] for r in records], vectors, valid)

    def teacher_forced(self, design: dict, target: dict) -> tuple[DesignMemory, list[tuple[StepResult, dict]]]:
        validate_target(design, target)
        encoded = self.encode(design)
        history, results = [], []
        operation, operation_index = "", -1
        for event in target["events"]:
            if event["action"] == "OP":
                operation, operation_index = event["operation"], operation_index + 1
            elif event["action"] == "PARAM":
                result = self.step(encoded, operation, event["parameter"], history, operation_index)
                results.append((result, event))
                history.append({**event, "operation": operation, "operation_index": operation_index})
        return encoded, results

    @torch.no_grad()
    def predict(self, design: dict, operation: str, parameter: str,
                records: list[dict], operation_index: int) -> dict:
        if self.training:
            raise RuntimeError("Local prediction requires model.eval()")
        if self.vocabulary.op_index.get(operation, 1) < 2 or self.vocabulary.param_index.get(parameter, 1) < 2:
            raise ValueError("Local query is outside the training vocabulary; no silent UNK prediction")
        encoded = self.encode(design)
        result = self.step(encoded, operation, parameter, records, operation_index)
        if any(not torch.isfinite(values).all() for values in result.heads.values()):
            raise ValueError("Nonfinite local predictions; no addresses returned")
        fields = {}
        for field, ids in (("features", encoded.feature_ids), ("requirements", encoded.requirement_ids),
                           ("sources", result.source_ids)):
            fields[field] = self.addressing.select(result.heads[field], ids)
        return {
            "operation": operation, "parameter": parameter, "addresses": fields,
            "roles": {r: p for r, p in zip(ROLES, result.heads["roles"].sigmoid().tolist())},
            "update": dict(zip(UPDATES, result.heads["update"].softmax(-1).tolist())),
            "status": "local_candidates_not_verified",
            "input_diagnostics": encoded.diagnostics,
            "numeric_policy": "Use exact records and execution tools, not local numeric proposals.",
        }
