from __future__ import annotations

from torch import Tensor, nn

from farmcad.models.pointer import TypedPointer, select_pointers


class AddressResolver(nn.Module):
    """Learned addressing only; this module does not own or mutate records."""

    def __init__(self, hidden: int, slots: int):
        super().__init__()
        self.features = TypedPointer(hidden, slots)
        self.requirements = TypedPointer(hidden, slots)
        self.sources = TypedPointer(hidden, slots)

    def forward(self, query: Tensor, features: Tensor, requirements: Tensor,
                states: Tensor, valid: list[bool]) -> dict[str, Tensor]:
        return {
            "features": self.features(query, features),
            "requirements": self.requirements(query, requirements),
            "sources": self.sources(query, states, valid),
        }

    @staticmethod
    def select(logits: Tensor, ids: list[str]) -> dict:
        return {
            "selected_ids": select_pointers(logits, ids),
            "candidate_ids": list(ids),
            "slot_probabilities_uncalibrated": logits.softmax(-1).tolist(),
            "special_columns": ["NONE", "UNKNOWN", "EXTERNAL"],
        }
