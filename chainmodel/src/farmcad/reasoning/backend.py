from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PlannerReply:
    status: str
    decision: dict = None


class PlannerBackend(Protocol):
    def request(self, packet: dict) -> PlannerReply:
        ...


class QwenPlaceholder:
    def __init__(self, model_name: str, model_path: str):
        self.model_name, self.model_path = model_name, model_path

    def describe(self) -> dict:
        return {
            "backend": "placeholder", "model_name": self.model_name, "model_path": self.model_path,
            "weights_loaded": False, "inference_ready": False,
            "note": "User-specified model label; download contents and serving compatibility have not been inspected.",
        }

    def request(self, packet: dict) -> PlannerReply:
        return PlannerReply("awaiting_llm")
