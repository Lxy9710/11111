from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Union

import torch

from farmcad.data.dataset import Case, Vocabulary, load_cases
from farmcad.models.model import FARMModel, ModelConfig
from farmcad.models.local import LocalPredictor
from farmcad.training.local_metrics import evaluate_local
from farmcad.training.losses import DEFAULT_LOSS_WEIGHTS, DEFAULT_QUALITY_WEIGHTS, training_loss, validate_weights
from farmcad.training.metrics import evaluate_case
from farmcad.utils import file_hash, finite, object_hash, read_json, require_keys, write_json


@dataclass
class TrainingConfig:
    seed: int = 17
    epochs: int = 100
    learning_rate: float = 0.0003
    weight_decay: float = 0.0001
    grad_clip: float = 1.0
    accumulate_cases: int = 1
    num_threads: int = 2
    device: str = "cpu"
    max_updates: int = 0
    patience: int = 10
    quality_weights: dict = field(default_factory=lambda: dict(DEFAULT_QUALITY_WEIGHTS))
    loss_weights: dict = field(default_factory=lambda: dict(DEFAULT_LOSS_WEIGHTS))

    @classmethod
    def from_dict(cls, data: dict) -> TrainingConfig:
        require_keys(data, {f.name for f in fields(cls)}, set(), "training config")
        config = cls(**data)
        for key in ("epochs", "accumulate_cases", "num_threads", "patience"):
            if isinstance(getattr(config, key), bool) or not isinstance(getattr(config, key), int) or getattr(config, key) <= 0:
                raise ValueError(f"Invalid positive integer: {key}")
        if not isinstance(config.seed, int) or config.seed < 0 or not isinstance(config.max_updates, int) or config.max_updates < 0:
            raise ValueError("Invalid seed/max_updates")
        if not finite(config.learning_rate) or config.learning_rate <= 0 or not finite(config.grad_clip) or config.grad_clip <= 0:
            raise ValueError("Invalid learning rate/gradient clip")
        if not finite(config.weight_decay) or config.weight_decay < 0:
            raise ValueError("Invalid weight decay")
        validate_weights(config.quality_weights, config.loss_weights)
        return config


def device_for(name: str) -> torch.device:
    device = torch.device(name)
    if device.type not in ("cpu", "cuda"):
        raise ValueError("Supported devices: cpu, cuda")
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable; choose cpu explicitly")
    return device


def dataset_fingerprint(cases: list[Case]) -> str:
    return object_hash([{
        "case_id": c.case_id, "family_id": c.family_id, "split": c.split,
        "design": c.design, "target": c.target,
    } for c in cases])


def pack_optimizer(value, tensors: dict) -> dict:
    # PyTorch 1.13's restricted loader cannot read BINFLOAT; keep scalars in JSON.
    if isinstance(value, torch.Tensor):
        key = str(len(tensors))
        tensors[key] = value
        return {"kind": "tensor", "key": key}
    if isinstance(value, dict):
        return {"kind": "dict", "items": [[k, pack_optimizer(v, tensors)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return {"kind": "tuple" if isinstance(value, tuple) else "list",
                "items": [pack_optimizer(v, tensors) for v in value]}
    if value is None or isinstance(value, (str, bool)) or finite(value):
        return {"kind": "scalar", "value": value}
    raise ValueError(f"Unsupported optimizer state type: {type(value).__name__}")


def unpack_optimizer(value: dict, tensors: dict):
    kind = value["kind"]
    if kind == "tensor":
        return tensors[value["key"]]
    if kind == "dict":
        return {k: unpack_optimizer(v, tensors) for k, v in value["items"]}
    if kind in ("tuple", "list"):
        items = [unpack_optimizer(v, tensors) for v in value["items"]]
        return tuple(items) if kind == "tuple" else items
    if kind == "scalar":
        scalar = value["value"]
        if scalar is None or isinstance(scalar, (str, bool)) or finite(scalar):
            return scalar
    raise ValueError("Invalid optimizer JSON structure")


def save_checkpoint(path: Path, model: Union[FARMModel, LocalPredictor], optimizer: torch.optim.Optimizer,
                    metadata: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    temporary = path / "weights.tmp"
    optimizer_tensors = {}
    optimizer_structure = pack_optimizer(optimizer.state_dict(), optimizer_tensors)
    torch.save({
        "model": model.state_dict(), "optimizer_tensors": optimizer_tensors,
        "torch_rng": torch.get_rng_state(),
        "cuda_rng": torch.cuda.get_rng_state_all() if next(model.parameters()).is_cuda else [],
    }, temporary)
    temporary.replace(path / "weights.pt")
    write_json(path / "metadata.json", {
        **metadata, "checkpoint_version": 1, "task": "T2",
        "architecture": "local" if isinstance(model, LocalPredictor) else "gru",
        "model_config": asdict(model.config), "vocabulary": model.vocabulary.to_dict(),
        "weights_sha256": file_hash(path / "weights.pt"), "torch_version": torch.__version__,
        "optimizer_structure": optimizer_structure,
    })


def build_model(architecture: str, config: ModelConfig, vocabulary: Vocabulary) -> Union[FARMModel, LocalPredictor]:
    if architecture == "gru":
        return FARMModel(config, vocabulary)
    if architecture == "local":
        return LocalPredictor(config, vocabulary)
    raise ValueError(f"Unsupported architecture: {architecture}")


def load_checkpoint(path: Path, device: str = "cpu") -> tuple[Union[FARMModel, LocalPredictor], dict, dict]:
    metadata = read_json(path / "metadata.json")
    if metadata["checkpoint_version"] != 1 or metadata["task"] != "T2":
        raise ValueError("Unsupported checkpoint")
    if file_hash(path / "weights.pt") != metadata["weights_sha256"]:
        raise ValueError("Checkpoint weight hash mismatch")
    vocabulary = Vocabulary(**metadata["vocabulary"])
    model = build_model(metadata.get("architecture", "gru"), ModelConfig.from_dict(metadata["model_config"]), vocabulary).to(device_for(device))
    payload = torch.load(path / "weights.pt", map_location=device_for(device), weights_only=True)
    payload["optimizer"] = unpack_optimizer(metadata["optimizer_structure"], payload["optimizer_tensors"])
    model.load_state_dict(payload["model"], strict=True)
    model.eval()
    return model, metadata, payload


@torch.no_grad()
def validate(model: Union[FARMModel, LocalPredictor], cases: list[Case], config: TrainingConfig) -> dict:
    model.eval()
    losses, rollouts, local_metrics = [], [], []
    for case in cases:
        loss, _ = training_loss(model, case.design, case.target, config.quality_weights, config.loss_weights)
        losses.append(float(loss.item()))
        if isinstance(model, LocalPredictor):
            local_metrics.append(evaluate_local(model, case.design, case.target, {"gold", "silver", "system"}))
        else:
            metrics, _ = evaluate_case(model, case.design, case.target, {"gold", "silver", "system"})
            rollouts.append(metrics)
    return {
        "teacher_forced_loss": sum(losses) / len(losses) if losses else None,
        "rollout_cases": rollouts,
        "local_cases": local_metrics,
        "evaluation_scope": "conditional_local_not_route_rollout" if isinstance(model, LocalPredictor) else "T2_rollout",
        "status": "validation_available" if losses else "smoke_only_no_independent_validation",
    }


def train(manifest: Path, config_path: Path, output: Path, smoke: bool = False,
          resume: Path = None, epochs: int = None, max_updates: int = None) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite run directory: {output}")
    config = read_json(config_path)
    require_keys(config, {"model", "training", "architecture"}, {"model", "training"}, "run config")
    architecture = config.get("architecture", "gru")
    if epochs is not None:
        config["training"]["epochs"] = epochs
    if max_updates is not None:
        config["training"]["max_updates"] = max_updates
    model_config = ModelConfig.from_dict(config["model"])
    training_config = TrainingConfig.from_dict(config["training"])
    torch.set_num_threads(training_config.num_threads)
    torch.manual_seed(training_config.seed)
    device = device_for(training_config.device)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(training_config.seed)
    cases = load_cases(manifest, smoke=smoke)
    vocabulary = Vocabulary.build(cases)
    fingerprint = dataset_fingerprint(cases)
    training = [case for case in cases if case.split == "train"]
    validation = [case for case in cases if case.split == "validation"]
    model = build_model(architecture, model_config, vocabulary).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=training_config.learning_rate, weight_decay=training_config.weight_decay)
    state = {"epoch": 0, "cursor": 0, "global_step": 0, "best_validation_loss": None, "bad_epochs": 0}
    if resume:
        model, previous, payload = load_checkpoint(resume, training_config.device)
        if previous["dataset_fingerprint"] != fingerprint or previous["vocabulary"] != vocabulary.to_dict():
            raise ValueError("Resume dataset/vocabulary changed; start a new training experiment")
        if previous["model_config"] != asdict(model_config) or previous.get("architecture", "gru") != architecture:
            raise ValueError("Resume architecture mismatch")
        old_training = dict(previous["training_config"])
        new_training = asdict(training_config)
        for key in ("epochs", "max_updates", "num_threads", "device"):
            old_training.pop(key)
            new_training.pop(key)
        if old_training != new_training:
            raise ValueError("Resume optimizer/supervision settings changed")
        optimizer = torch.optim.AdamW(model.parameters(), lr=training_config.learning_rate, weight_decay=training_config.weight_decay)
        optimizer.load_state_dict(payload["optimizer"])
        torch.set_rng_state(payload["torch_rng"].cpu())
        if device.type == "cuda" and payload["cuda_rng"]:
            torch.cuda.set_rng_state_all([s.cpu() for s in payload["cuda_rng"]])
        state = previous["training_state"]
    if state["epoch"] >= training_config.epochs or (
        training_config.max_updates and state["global_step"] >= training_config.max_updates
    ):
        raise ValueError("No remaining training budget; increase epochs/max_updates")
    output.mkdir(parents=True)
    logger = logging.getLogger("farmcad.train")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(output / "training.log", encoding="utf-8")
    logger.addHandler(handler)
    coverage = vocabulary.coverage(cases)
    write_json(output / "config.resolved.json", {"architecture": architecture, "model": asdict(model_config), "training": asdict(training_config)})
    write_json(output / "dataset_report.json", {
        "task": "T2", "architecture": architecture, "smoke": smoke, "dataset_fingerprint": fingerprint,
        "cases_by_split": {split: [c.case_id for c in cases if c.split == split] for split in ("train", "validation", "test")},
        "vocabulary_coverage": coverage, "source_manifest": str(manifest.resolve()),
        "source_manifest_sha256": file_hash(manifest), "parameter_count": sum(p.numel() for p in model.parameters()),
        "source_code": [
            {"path": str(p.relative_to(Path(__file__).resolve().parents[1])), "sha256": file_hash(p)}
            for p in sorted(Path(__file__).resolve().parents[1].rglob("*.py"))
        ],
    })
    for split, counts in coverage.items():
        if any(counts.values()):
            logger.warning("Closed-vocabulary OOV in %s: %s", split, counts)
    metadata = {
        "dataset_fingerprint": fingerprint, "training_config": asdict(training_config),
        "smoke": smoke, "training_state": state,
    }
    validation_report = {"status": "not_evaluated", "teacher_forced_loss": None, "rollout_cases": []}
    history = []
    try:
        while state["epoch"] < training_config.epochs:
            order = list(range(len(training)))
            random.Random(training_config.seed + state["epoch"]).shuffle(order)
            model.train()
            while state["cursor"] < len(order):
                indices = order[state["cursor"]:state["cursor"] + training_config.accumulate_cases]
                optimizer.zero_grad(set_to_none=True)
                batch_loss = 0.0
                for index in indices:
                    case = training[index]
                    loss, detail = training_loss(model, case.design, case.target,
                                                 training_config.quality_weights, training_config.loss_weights)
                    (loss / len(indices)).backward()
                    batch_loss += float(loss.item()) / len(indices)
                    if state["global_step"] == 0 and any(detail["input_diagnostics"].values()):
                        logger.warning("Input text window truncation %s: %s", case.case_id, detail["input_diagnostics"])
                norm = torch.nn.utils.clip_grad_norm_(model.parameters(), training_config.grad_clip)
                if not torch.isfinite(norm):
                    raise RuntimeError("Nonfinite gradients; checkpoint not advanced")
                optimizer.step()
                state["cursor"] += len(indices)
                state["global_step"] += 1
                row = {"step": state["global_step"], "epoch": state["epoch"], "loss": batch_loss,
                       "gradient_norm": float(norm.item()), "cases": [training[i].case_id for i in indices]}
                history.append(row)
                with (output / "metrics.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                logger.info("step=%d loss=%.6f", state["global_step"], batch_loss)
                if training_config.max_updates and state["global_step"] >= training_config.max_updates:
                    break
            if state["cursor"] == len(order):
                state["epoch"] += 1
                state["cursor"] = 0
                validation_report = validate(model, validation, training_config)
                score = validation_report["teacher_forced_loss"]
                if score is not None:
                    if state["best_validation_loss"] is None or score < state["best_validation_loss"]:
                        state["best_validation_loss"] = score
                        state["bad_epochs"] = 0
                        save_checkpoint(output / "checkpoints" / "best", model, optimizer, metadata)
                    else:
                        state["bad_epochs"] += 1
            save_checkpoint(output / "checkpoints" / "last", model, optimizer, metadata)
            if state["bad_epochs"] >= training_config.patience:
                break
            if training_config.max_updates and state["global_step"] >= training_config.max_updates:
                break
        write_json(output / "validation.json", validation_report)
        report = {
            "status": "completed", "task": "T2", "architecture": architecture, "smoke": smoke, "new_updates": len(history),
            "global_step": state["global_step"], "first_loss": history[0]["loss"],
            "last_loss": history[-1]["loss"], "best_validation_loss": state["best_validation_loss"],
            "generalization_claim": False,
            "checkpoint": str((output / "checkpoints" / "last").resolve()),
            "note": "Smoke training verifies execution only; independent gold evaluation is required.",
        }
        write_json(output / "summary.json", report)
        return report
    finally:
        logger.removeHandler(handler)
        handler.close()
