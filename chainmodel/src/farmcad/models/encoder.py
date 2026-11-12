from __future__ import annotations

import math
from dataclasses import dataclass

import torch
from torch import Tensor, nn

from farmcad.data.schema import ENTITY_TYPES, VALUE_KEYS
from farmcad.utils import scaled


def value_vector(value: dict) -> list[float]:
    known = value["unit"] != "unknown"
    values = [value.get(k) if known else None for k in VALUE_KEYS]
    return [scaled(v) if v is not None else 0.0 for v in values] + [float(v is not None) for v in values]


class ByteTextEncoder(nn.Module):
    """Small offline baseline; no external model or tokenizer downloads."""

    def __init__(self, hidden: int, max_bytes: int):
        super().__init__()
        self.max_bytes = max_bytes
        self.embedding = nn.Embedding(257, hidden, padding_idx=0)
        self.conv = nn.Conv1d(hidden, hidden, 3, padding=1)

    def forward(self, texts: list[str], device: torch.device) -> Tensor:
        if not texts:
            return self.embedding.weight.new_zeros((0, self.embedding.embedding_dim))
        encoded = [[b + 1 for b in text.encode("utf-8")[:self.max_bytes]] for text in texts]
        width = max(1, max(len(row) for row in encoded))
        tokens = torch.zeros((len(texts), width), dtype=torch.long, device=device)
        for index, row in enumerate(encoded):
            if row:
                tokens[index, :len(row)] = torch.tensor(row, dtype=torch.long, device=device)
        mask = (tokens != 0).unsqueeze(-1)
        hidden = torch.tanh(self.conv(self.embedding(tokens).transpose(1, 2))).transpose(1, 2)
        return (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)


class RelationLayer(nn.Module):
    def __init__(self, hidden: int, dropout: float):
        super().__init__()
        self.message = nn.Linear(hidden, hidden)
        self.self_layer = nn.Linear(hidden, hidden)
        self.relations = nn.Embedding(3, hidden)
        self.norm = nn.LayerNorm(hidden)
        self.dropout = nn.Dropout(dropout)

    def forward(self, h: Tensor, edges: Tensor) -> Tensor:
        aggregate = torch.zeros_like(h)
        degree = h.new_zeros((h.shape[0], 1))
        if edges.numel():
            source, target, relation = edges.unbind(1)
            messages = self.message(h[source]) + self.relations(relation)
            aggregate.index_add_(0, target, messages)
            degree.index_add_(0, target, h.new_ones((target.shape[0], 1)))
        update = self.self_layer(h) + aggregate / degree.clamp(min=1)
        return self.norm(h + self.dropout(torch.nn.functional.gelu(update)))


@dataclass
class DesignMemory:
    entities: Tensor
    features: Tensor
    requirements: Tensor
    context: Tensor
    membership_logits: Tensor
    feature_ids: list[str]
    requirement_ids: list[str]
    diagnostics: dict


class DesignEncoder(nn.Module):
    def __init__(self, hidden: int, layers: int, dropout: float, text_bytes: int):
        super().__init__()
        self.hidden = hidden
        self.text = ByteTextEncoder(hidden, text_bytes)
        self.entity_types = nn.Embedding(len(ENTITY_TYPES), hidden)
        self.entity_numeric = nn.Linear(12, hidden)
        self.feature_numeric = nn.Linear(8, hidden)
        self.feature_units = nn.Embedding(3, hidden)
        self.requirement_numeric = nn.Linear(2, hidden)
        self.layers = nn.ModuleList([RelationLayer(hidden, dropout) for _ in range(layers)])
        self.feature_query = nn.Linear(hidden, hidden)
        self.entity_key = nn.Linear(hidden, hidden)
        self.feature_norm = nn.LayerNorm(hidden)
        self.context_norm = nn.LayerNorm(hidden)
        self.context_token = nn.Parameter(torch.zeros(hidden))

    def forward(self, design: dict) -> DesignMemory:
        device = self.context_token.device
        entities, features, requirements = design["entities"], design["features"], design["requirements"]
        numeric = torch.tensor([e["numeric"] for e in entities], dtype=torch.float32, device=device)
        types = torch.tensor([ENTITY_TYPES.index(e["type"]) for e in entities], dtype=torch.long, device=device)
        h = self.entity_numeric(numeric) + self.entity_types(types) + self.text([e["text"] for e in entities], device)
        edges = torch.tensor(design["edges"], dtype=torch.long, device=device).reshape(-1, 3)
        for layer in self.layers:
            h = layer(h, edges)
        feature_ids = [f["id"] for f in features]
        requirement_ids = [r["id"] for r in requirements]
        entity_index = {e["id"]: i for i, e in enumerate(entities)}
        if features:
            f = self.feature_numeric(torch.tensor(
                [value_vector(item["value"]) for item in features], dtype=torch.float32, device=device,
            )) + self.text([item["text"] for item in features], device)
            f = f + self.feature_units(torch.tensor(
                [("unknown", "mm", "um").index(item["value"]["unit"]) for item in features],
                dtype=torch.long, device=device,
            ))
            anchored = []
            for item in features:
                indices = [entity_index[i] for i in item["anchor_entities"]]
                anchored.append(h[indices].mean(0) if indices else h.new_zeros(self.hidden))
            f = f + torch.stack(anchored)
            membership = self.feature_query(f) @ self.entity_key(h).T / math.sqrt(self.hidden)
            f = self.feature_norm(f + torch.softmax(membership, dim=-1) @ h)
        else:
            f = h.new_zeros((0, self.hidden))
            membership = h.new_zeros((0, len(entities)))
        if requirements:
            r = self.requirement_numeric(torch.tensor(
                [[scaled(item["lower"]), scaled(item["upper"])] for item in requirements],
                dtype=torch.float32, device=device,
            )) + self.text([item["text"] for item in requirements], device)
            r = r + torch.stack([f[feature_ids.index(item["feature_id"])] for item in requirements])
        else:
            r = h.new_zeros((0, self.hidden))
        context = self.context_norm(
            self.context_token + h.mean(0) + (f.mean(0) if len(features) else 0)
            + self.text([design["context"]], device)[0]
        )
        diagnostics = {
            "entity_texts_truncated": sum(len(e["text"].encode("utf-8")) > self.text.max_bytes for e in entities),
            "context_text_truncated": len(design["context"].encode("utf-8")) > self.text.max_bytes,
            "feature_texts_truncated": sum(len(f["text"].encode("utf-8")) > self.text.max_bytes for f in features),
        }
        return DesignMemory(h, f, r, context, membership, feature_ids, requirement_ids, diagnostics)
