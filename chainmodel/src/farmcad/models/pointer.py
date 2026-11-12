from __future__ import annotations

import math

import torch
from torch import Tensor, nn

from farmcad.data.schema import SPECIALS


class TypedPointer(nn.Module):
    def __init__(self, hidden: int, slots: int):
        super().__init__()
        self.slots = slots
        self.specials = nn.Parameter(torch.randn(len(SPECIALS), hidden) * 0.02)
        self.slot_queries = nn.Parameter(torch.randn(slots, hidden) * 0.02)
        self.query = nn.Linear(hidden, hidden)
        self.key = nn.Linear(hidden, hidden)

    def forward(self, query: Tensor, candidates: Tensor, valid: list[bool] = None) -> Tensor:
        keys = torch.cat([self.specials, candidates], dim=0)
        logits = (self.query(query).unsqueeze(0) + self.slot_queries) @ self.key(keys).T / math.sqrt(query.shape[-1])
        if valid is not None:
            mask = torch.tensor([True] * len(SPECIALS) + valid, dtype=torch.bool, device=query.device)
            logits = logits.masked_fill(~mask.unsqueeze(0), -1e9)
        return logits

    def pool(self, choices: list[str], ids: list[str], candidates: Tensor) -> Tensor:
        vectors = [
            self.specials[SPECIALS.index(item)] if item in SPECIALS else candidates[ids.index(item)]
            for item in choices
        ]
        return torch.stack(vectors).mean(0) if vectors else self.specials[0]


def pointer_targets(value: list[str], ids: list[str], slots: int) -> list[int]:
    choices = ["UNKNOWN"] if value is None else value
    if len(choices) > slots:
        raise ValueError(f"Pointer set size {len(choices)} exceeds configured slots {slots}")
    indices = sorted(SPECIALS.index(item) if item in SPECIALS else len(SPECIALS) + ids.index(item) for item in choices)
    return indices + [0] * (slots - len(indices))


def select_pointers(logits: Tensor, ids: list[str]) -> list[str]:
    selected = []
    for row in logits:
        scores = row.clone()
        for item in selected:
            if item not in SPECIALS:
                scores[len(SPECIALS) + ids.index(item)] = -1e9
        index = int(scores.argmax().item())
        if index < len(SPECIALS):
            if not selected and index != 0:
                selected.append(SPECIALS[index])
            break
        selected.append(ids[index - len(SPECIALS)])
    return selected
