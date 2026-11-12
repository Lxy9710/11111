from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from farmcad.data.annotation import export_worksheets, import_worksheets
from farmcad.data.assist import annotate_assist
from farmcad.data.dataset import Vocabulary, load_cases
from farmcad.data.prepare import prepare_pilot, prepare_raw_design
from farmcad.data.schema import validate_design
from farmcad.models.local import LocalPredictor
from farmcad.models.model import FARMModel
from farmcad.reasoning.runtime import apply_response, initialize, open_session
from farmcad.training.local_metrics import evaluate_local
from farmcad.training.metrics import evaluate_case
from farmcad.training.trainer import load_checkpoint, train
from farmcad.utils import file_hash, read_json, write_json


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="FARM-CAD T2 process-chain generation")
    commands = root.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Separate a verified pilot's input and training targets")
    prepare.add_argument("--pilot", type=Path, required=True)
    prepare.add_argument("--family-id", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--annotations", type=Path)
    raw = commands.add_parser("prepare-design", help="Extract inference input only; no process engine or route lookup")
    raw.add_argument("--drawing-json", type=Path, required=True)
    raw.add_argument("--project-root", type=Path, required=True)
    raw.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("validate-data")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--smoke", action="store_true")
    fit = commands.add_parser("train")
    fit.add_argument("--manifest", type=Path, required=True)
    fit.add_argument("--config", type=Path, required=True)
    fit.add_argument("--output", type=Path, required=True)
    fit.add_argument("--smoke", action="store_true")
    fit.add_argument("--resume", type=Path)
    fit.add_argument("--epochs", type=int)
    fit.add_argument("--max-updates", type=int)
    predict = commands.add_parser("predict")
    predict.add_argument("--checkpoint", type=Path, required=True)
    predict.add_argument("--design", type=Path, required=True)
    predict.add_argument("--output", type=Path, required=True)
    predict.add_argument("--device", default="cpu")
    predict.add_argument("--max-events", type=int)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--checkpoint", type=Path, required=True)
    evaluate.add_argument("--manifest", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--split", choices=("train", "validation", "test"), default="test")
    evaluate.add_argument("--smoke", action="store_true")
    evaluate.add_argument("--include-weak", action="store_true")
    evaluate.add_argument("--device", default="cpu")
    evaluate.add_argument(
        "--eval-mode", choices=("teacher_forced", "self_predicted", "both"), default="both",
        help="teacher_forced 注入金标参数（仅上界）；self_predicted 用模型自身预测驱动查询（部署口径）。"
             "both 同时报告两种口径。")
    hybrid = commands.add_parser("hybrid-init", help="Create design-only memory and a bounded placeholder LLM request")
    hybrid.add_argument("--design", type=Path, required=True)
    hybrid.add_argument("--checkpoint", type=Path, required=True)
    hybrid.add_argument("--config", type=Path, required=True)
    hybrid.add_argument("--output", type=Path, required=True)
    hybrid.add_argument("--smoke", action="store_true")
    step = commands.add_parser("hybrid-step", help="Apply an explicit offline decision; no real LLM call")
    step.add_argument("--session", type=Path, required=True)
    step.add_argument("--decision", type=Path, required=True)
    step.add_argument("--output", type=Path, required=True)
    pending = commands.add_parser("hybrid-pending", help="Recover the pending request from persistent memory")
    pending.add_argument("--session", type=Path, required=True)
    sheet = commands.add_parser(
        "annotate-export",
        help="Write engineer worksheets bound to the current design/target hashes")
    sheet.add_argument("--case-dir", type=Path, required=True)
    sheet.add_argument("--output", type=Path, required=True)
    merge = commands.add_parser(
        "annotate-import",
        help="Merge reviewed worksheets into a new gold-labelled case directory")
    merge.add_argument("--case-dir", type=Path, required=True)
    merge.add_argument("--worksheets", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--reviewer", default="")
    assist = commands.add_parser(
        "annotate-assist",
        help="Add the local model's per-parameter suggestions to an engineer worksheet")
    assist.add_argument("--case-dir", type=Path, required=True)
    assist.add_argument("--checkpoint", type=Path, required=True)
    assist.add_argument("--output", type=Path, required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    if args.command == "prepare":
        result = prepare_pilot(args.pilot, args.output, args.family_id, args.annotations)
    elif args.command == "prepare-design":
        prepare_raw_design(args.drawing_json, args.project_root, args.output)
        result = {"status": "prepared_design_only", "output": str(args.output.resolve())}
    elif args.command == "validate-data":
        cases = load_cases(args.manifest, args.smoke)
        vocabulary = Vocabulary.build(cases)
        result = {"status": "valid", "cases": len(cases), "smoke": args.smoke,
                  "vocabulary_coverage": vocabulary.coverage(cases)}
    elif args.command == "train":
        result = train(args.manifest, args.config, args.output, args.smoke, args.resume, args.epochs, args.max_updates)
    elif args.command == "hybrid-init":
        result = initialize(args.design, args.checkpoint, args.config, args.output, args.smoke)
    elif args.command == "hybrid-step":
        result = apply_response(args.session, args.decision, args.output)
    elif args.command == "hybrid-pending":
        session = open_session(args.session)
        with session.store:
            result = {"request": session.pending(), "backend": session.backend.describe()}
    elif args.command == "annotate-export":
        result = export_worksheets(args.case_dir, args.output)
    elif args.command == "annotate-import":
        result = import_worksheets(args.case_dir, args.worksheets, args.output, args.reviewer)
    elif args.command == "annotate-assist":
        result = annotate_assist(args.case_dir, args.checkpoint, args.output)
    elif args.command == "predict":
        if args.output.exists():
            raise FileExistsError(f"Refusing to overwrite prediction: {args.output}")
        model, metadata, _ = load_checkpoint(args.checkpoint, args.device)
        if not isinstance(model, FARMModel):
            raise ValueError("Local checkpoints do not generate routes; use hybrid-init")
        if args.max_events is not None:
            if args.max_events < 1:
                raise ValueError("max-events must be positive")
            model.config.max_events = args.max_events
        design = read_json(args.design)
        validate_design(design)
        prediction = model.generate(design)
        prediction["checkpoint_smoke"] = metadata["smoke"]
        prediction["input_sha256"] = file_hash(args.design)
        write_json(args.output, prediction)
        result = {"status": "prediction_written", "events": len(prediction["events"]),
                  "termination": prediction["termination"], "structurally_complete": prediction["structurally_complete"],
                  "engineering_verified": False, "output": str(args.output.resolve())}
    else:
        if args.output.exists():
            raise FileExistsError(f"Refusing to overwrite evaluation: {args.output}")
        if args.split == "train" and not args.smoke:
            raise ValueError("Training-split evaluation requires --smoke and is not generalization evidence")
        cases = load_cases(args.manifest, args.smoke)
        selected = [c for c in cases if c.split == args.split]
        if not selected:
            raise ValueError(f"No cases in requested split: {args.split}")
        model, metadata, _ = load_checkpoint(args.checkpoint, args.device)
        qualities = {"gold", "silver", "system"} if args.include_weak else {"gold"}
        modes = ("teacher_forced", "self_predicted") if args.eval_mode == "both" else (args.eval_mode,)
        if isinstance(model, LocalPredictor):
            by_mode = {
                mode: [evaluate_local(model, c.design, c.target, qualities, mode) for c in selected]
                for mode in modes
            }
            metrics = by_mode["teacher_forced"] if len(modes) == 1 else []
        else:
            by_mode = {}
            metrics = [evaluate_case(model, c.design, c.target, qualities)[0] for c in selected]
        report = {
            "task": "T2", "split": args.split, "smoke": args.smoke,
            "checkpoint_smoke": metadata["smoke"], "label_qualities": sorted(qualities),
            "scope": "conditional_local_not_route_rollout" if isinstance(model, LocalPredictor) else "T2_rollout",
            "cases": metrics,
            "by_evaluation_mode": by_mode,
            "note": "Null metrics indicate absent supervision, not perfect predictions. "
                    "teacher_forced injects the gold parameter into the query and therefore "
                    "reports an upper bound only; self_predicted is the deployable measure.",
        }
        write_json(args.output, report)
        result = {"status": "evaluation_written", "cases": len(metrics), "label_qualities": sorted(qualities)}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
