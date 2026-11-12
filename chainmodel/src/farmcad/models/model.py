from __future__ import annotations

import math
from dataclasses import dataclass, field, fields

import torch
from torch import Tensor, nn

from farmcad.data.dataset import Vocabulary
from farmcad.data.schema import ACTIONS, ROLES, SPECIALS, UPDATES, VALUE_KEYS, label, unknown_value, validate_design, validate_target
from farmcad.models.encoder import DesignEncoder, DesignMemory, value_vector
from farmcad.models.pointer import TypedPointer, pointer_targets, select_pointers
from farmcad.utils import require_keys, unscaled


@dataclass
class ModelConfig:
    hidden: int = 128
    graph_layers: int = 3
    dropout: float = 0.1
    text_bytes: int = 256
    pointer_slots: int = 4
    max_entities: int = 2000
    max_events: int = 512
    max_operations: int = 64
    max_parameters: int = 32
    role_threshold: float = 0.5

    @classmethod
    def from_dict(cls, data: dict) -> ModelConfig:
        require_keys(data, {f.name for f in fields(cls)}, set(), "model config")
        config = cls(**data)
        for key in ("hidden", "graph_layers", "text_bytes", "pointer_slots", "max_entities", "max_events", "max_operations", "max_parameters"):
            if isinstance(getattr(config, key), bool) or not isinstance(getattr(config, key), int) or getattr(config, key) < 1:
                raise ValueError(f"Invalid positive integer: {key}")
        if not 0 <= config.dropout < 1 or not 0 < config.role_threshold < 1:
            raise ValueError("Invalid dropout/role threshold")
        return config


@dataclass
class StateEntry:
    id: str
    operation_index: int
    hidden: Tensor
    value: dict
    value_status: str


@dataclass
class DecoderState:
    hidden: Tensor
    previous: Tensor
    operation_index: int = -1
    operation_token: int = 0
    in_operation: bool = False
    parameter_count: int = 0
    memory: list[StateEntry] = field(default_factory=list)


@dataclass
class StepResult:
    heads: dict[str, Tensor]
    hidden: Tensor
    source_ids: list[str]
    source_vectors: Tensor
    source_valid: list[bool]


class FARMModel(nn.Module):
    def __init__(self, config: ModelConfig, vocabulary: Vocabulary):
        super().__init__()
        self.config, self.vocabulary = config, vocabulary
        h = config.hidden
        self.encoder = DesignEncoder(h, config.graph_layers, config.dropout, config.text_bytes)
        self.action_embedding = nn.Embedding(len(ACTIONS) + 1, h)
        self.operation_embedding = nn.Embedding(len(vocabulary.operations), h)
        self.parameter_embedding = nn.Embedding(len(vocabulary.parameters), h)
        self.decoder = nn.GRUCell(h * 3, h)
        self.state_query = nn.Linear(h, h)
        self.state_key = nn.Linear(h, h)
        self.action_head = nn.Linear(h, len(ACTIONS))
        self.operation_head = nn.Linear(h, len(vocabulary.operations))
        self.parameter_head = nn.Linear(h, len(vocabulary.parameters))
        self.role_head = nn.Linear(h, len(ROLES))
        self.update_head = nn.Linear(h, len(UPDATES))
        self.value_head = nn.Linear(h, 4)
        self.value_mask_head = nn.Linear(h, 4)
        self.unit_head = nn.Linear(h, 2)
        self.feature_pointer = TypedPointer(h, config.pointer_slots)
        self.requirement_pointer = TypedPointer(h, config.pointer_slots)
        self.source_pointer = TypedPointer(h, config.pointer_slots)
        self.value_embedding = nn.Linear(8, h)
        self.unit_embedding = nn.Embedding(3, h)
        self.role_embedding = nn.Linear(len(ROLES), h)
        self.update_embedding = nn.Embedding(len(UPDATES), h)
        self.memory_writer = nn.Sequential(nn.Linear(h * 5, h), nn.GELU(), nn.LayerNorm(h))

    def encode(self, design: dict) -> DesignMemory:
        validate_design(design)
        if len(design["entities"]) > self.config.max_entities:
            raise ValueError("Design exceeds max_entities; no silent entity truncation")
        return self.encoder(design)

    def initial_state(self, memory: DesignMemory) -> DecoderState:
        return DecoderState(memory.context, self.action_embedding.weight[-1])

    def step(self, state: DecoderState, design: DesignMemory, forced: dict = None) -> StepResult:
        source_ids = [s.id for s in state.memory]
        source_vectors = torch.stack([s.hidden for s in state.memory]) if state.memory else design.context.new_zeros((0, self.config.hidden))
        valid = [s.operation_index < state.operation_index for s in state.memory]
        eligible = [i for i, flag in enumerate(valid) if flag]
        dynamic = design.context.new_zeros(self.config.hidden)
        if eligible:
            keys = source_vectors[eligible]
            attention = self.state_query(state.hidden) @ self.state_key(keys).T / math.sqrt(self.config.hidden)
            dynamic = torch.softmax(attention, dim=-1) @ keys
        h = self.decoder(torch.cat([state.previous, design.context, dynamic]), state.hidden)
        action = self.action_head(h)
        allowed = [False] * len(ACTIONS)
        if state.in_operation:
            allowed[ACTIONS.index("PARAM")] = state.parameter_count < self.config.max_parameters and len(self.vocabulary.parameters) > 2
            allowed[ACTIONS.index("END_OP")] = True
        else:
            allowed[ACTIONS.index("OP")] = state.operation_index + 1 < self.config.max_operations
            allowed[ACTIONS.index("EOS")] = state.operation_index >= 0
        action = action.masked_fill(~torch.tensor(allowed, device=h.device), -1e9)
        operation = self.operation_head(h)
        parameter = self.parameter_head(h + self.operation_embedding.weight[state.operation_token])
        operation = operation.masked_fill(torch.arange(len(operation), device=h.device) < 2, -1e9)
        parameter = parameter.masked_fill(torch.arange(len(parameter), device=h.device) < 2, -1e9)
        param_id = self.vocabulary.param_index.get(forced["parameter"], 1) if forced and forced["action"] == "PARAM" else int(parameter.argmax().item())
        query = h + self.parameter_embedding.weight[param_id]
        return StepResult({
            "action": action, "operation": operation, "parameter": parameter,
            "features": self.feature_pointer(query, design.features),
            "requirements": self.requirement_pointer(query, design.requirements),
            "sources": self.source_pointer(query, source_vectors, valid),
            "roles": self.role_head(query), "update": self.update_head(query),
            "value": self.value_head(query), "value_mask": self.value_mask_head(query),
            "unit": self.unit_head(query),
        }, h, source_ids, source_vectors, valid)

    def advance(self, state: DecoderState, encoded: DesignMemory, result: StepResult,
                event: dict, value_status: str = "teacher_forced_history") -> None:
        state.hidden = result.hidden
        action = event["action"]
        previous = self.action_embedding.weight[ACTIONS.index(action)]
        if action == "OP":
            state.operation_index += 1
            state.operation_token = self.vocabulary.op_index.get(event["operation"], 1)
            state.in_operation, state.parameter_count = True, 0
            previous = previous + self.operation_embedding.weight[state.operation_token]
        elif action == "END_OP":
            state.in_operation = False
        elif action == "PARAM":
            labels = event["labels"]
            choices = lambda key: labels[key]["value"] if labels[key]["value"] is not None else ["UNKNOWN"]
            features = self.feature_pointer.pool(choices("features"), encoded.feature_ids, encoded.features)
            requirements = self.requirement_pointer.pool(choices("requirements"), encoded.requirement_ids, encoded.requirements)
            sources = self.source_pointer.pool(choices("sources"), result.source_ids, result.source_vectors)
            value = labels["value"]["value"] or unknown_value()
            role_values = labels["roles"]["value"] or []
            update = labels["update"]["value"] or "unknown"
            attribute = (
                self.parameter_embedding.weight[self.vocabulary.param_index.get(event["parameter"], 1)]
                + self.operation_embedding.weight[state.operation_token]
                + self.value_embedding(result.hidden.new_tensor(value_vector(value)))
                + self.unit_embedding.weight[("unknown", "mm", "um").index(value["unit"])]
                + self.role_embedding(result.hidden.new_tensor([float(r in role_values) for r in ROLES]))
                + self.update_embedding.weight[UPDATES.index(update)]
            )
            hidden = self.memory_writer(torch.cat([result.hidden, features, requirements, sources, attribute]))
            state.memory.append(StateEntry(event["id"], state.operation_index, hidden, value, value_status))
            state.parameter_count += 1
            previous = previous + attribute
        state.previous = previous

    def teacher_forced(self, design: dict, target: dict) -> tuple[DesignMemory, list[tuple[StepResult, dict]]]:
        validate_target(design, target)
        if len(target["events"]) > self.config.max_events:
            raise ValueError("Target exceeds max_events; no silent target truncation")
        encoded = self.encode(design)
        state = self.initial_state(encoded)
        results = []
        for event in target["events"]:
            result = self.step(state, encoded, event)
            if result.heads["action"][ACTIONS.index(event["action"])].item() <= -1e8:
                raise ValueError("Target violates decoder limits/grammar; increase limits or correct labels")
            if event["action"] == "PARAM":
                for name, ids in (("features", encoded.feature_ids), ("requirements", encoded.requirement_ids), ("sources", result.source_ids)):
                    pointer_targets(event["labels"][name]["value"], ids, self.config.pointer_slots)
            results.append((result, event))
            self.advance(state, encoded, result, event)
        return encoded, results

    def resolve_value(self, design: dict, state: DecoderState, features: list[str],
                      sources: list[str], update: str) -> tuple[dict, str]:
        if update == "design_copy":
            if len(features) == 1 and features[0] not in SPECIALS:
                value = next(f["value"] for f in design["features"] if f["id"] == features[0])
                return dict(value), "copied_design_value_not_engineering_validation"
            return unknown_value(), "unresolved_design_pointer"
        if update == "state_copy":
            if len(sources) == 1 and sources[0] not in SPECIALS:
                parent = next(s for s in state.memory if s.id == sources[0])
                if parent.operation_index >= state.operation_index:
                    raise ValueError("Attempted noncausal state copy")
                return dict(parent.value), "copied_state:" + parent.value_status
            return unknown_value(), "unresolved_state_pointer"
        return unknown_value(), "requires_rule_execution_or_review"

    @torch.no_grad()
    def generate(self, design: dict) -> dict:
        if self.training:
            raise RuntimeError("Call model.eval() before autoregressive inference")
        encoded = self.encode(design)
        state = self.initial_state(encoded)
        events = []
        termination = "max_events"
        limits = set()
        for index in range(self.config.max_events):
            if state.in_operation and state.parameter_count >= self.config.max_parameters:
                limits.add("max_parameters")
            if not state.in_operation and state.operation_index + 1 >= self.config.max_operations:
                limits.add("max_operations")
            result = self.step(state, encoded)
            heads = result.heads
            action = ACTIONS[int(heads["action"].argmax().item())]
            event = {"id": f"generated:event:{index}", "action": action, "quality": "unknown"}
            rendered = {"id": event["id"], "action": action}
            value_status = "not_applicable"
            if action == "OP":
                event["operation"] = self.vocabulary.operations[int(heads["operation"].argmax().item())]
                event["parameters_complete"] = False
                rendered["operation"] = event["operation"]
            elif action == "PARAM":
                event["parameter"] = self.vocabulary.parameters[int(heads["parameter"].argmax().item())]
                features = select_pointers(heads["features"], encoded.feature_ids)
                requirements = select_pointers(heads["requirements"], encoded.requirement_ids)
                sources = select_pointers(heads["sources"], result.source_ids)
                roles = [r for r, probability in zip(ROLES, heads["roles"].sigmoid().tolist()) if probability >= self.config.role_threshold]
                update = UPDATES[int(heads["update"].argmax().item())]
                if update == "external":
                    sources = ["EXTERNAL"]
                value, value_status = self.resolve_value(design, state, features, sources, update)
                event["labels"] = {
                    key: label(v) for key, v in (
                        ("features", features), ("requirements", requirements), ("sources", sources),
                        ("roles", roles), ("update", update), ("value", value),
                    )
                }
                proposed = {
                    k: unscaled(v) if probability >= 0.5 else None
                    for k, v, probability in zip(VALUE_KEYS, heads["value"].tolist(), heads["value_mask"].sigmoid().tolist())
                }
                rendered.update(
                    parameter=event["parameter"], feature_ids=features, requirement_ids=requirements,
                    source_state_ids=sources, roles=roles, update=update, value=value,
                    value_status=value_status,
                    numeric_proposal={"values": proposed, "unit": ("mm", "um")[int(heads["unit"].argmax().item())],
                                      "status": "unverified_not_executed"},
                )
            rendered["action_confidence_uncalibrated"] = float(heads["action"].softmax(-1).max().item())
            events.append(rendered)
            self.advance(state, encoded, result, event, value_status)
            if action == "EOS":
                termination = "eos"
                break
        memberships = []
        for fid, probabilities in zip(encoded.feature_ids, encoded.membership_logits.sigmoid().tolist()):
            members = [e["id"] for e, p in zip(design["entities"], probabilities)
                       if p >= 0.5 and e["type"] not in ("DIMENSION", "TEXT", "MTEXT")]
            memberships.append({"feature_id": fid, "entity_ids": members, "status": "predicted_not_human_verified"})
        return {
            "schema_version": "farmcad-prediction-v1", "task": "T2", "case_id": design["case_id"],
            "termination": termination, "limits_reached": sorted(limits),
            "structurally_complete": termination == "eos" and not limits,
            "engineering_verified": False, "events": events, "feature_memberships": memberships,
            "input_diagnostics": encoded.diagnostics,
            "limitations": [
                "Feature discovery is conditioned on design-only parameter seeds.",
                "Operation and parameter catalogues are learned from training data only.",
                "Lookup/derive values require rule execution or manual review; proposals are not executable results.",
                "Model confidence is uncalibrated; structural completion is not manufacturing correctness.",
            ],
        }
