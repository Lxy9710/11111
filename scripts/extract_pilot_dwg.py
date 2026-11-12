"""Extract a private DWG copy with the selected Process Hub implementation.

Run with the project's Python environment. Refuses to run alongside another
AutoCAD process; never changes the source DWG or the project's configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--drawing", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = args.project_root.resolve(strict=True)
    drawing = args.drawing.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{output}")
    sys.path.insert(0, str(project))

    import psutil
    import pythoncom

    from src.TQ.cad_scheduler.access import allow_direct_cad
    from src.TQ.dag_process.autocad_utils import (
        close_autocad_after_dwg,
        connect_autocad,
        describe_autocad_instance,
    )
    from src.TQ.dag_process.extract_from_dwg import extract_single_dwg

    existing = [
        p.pid for p in psutil.process_iter(["name"])
        if (p.info["name"] or "").lower() == "acad.exe"
    ]
    if existing:
        raise RuntimeError(f"发现正在运行的 AutoCAD，停止以免干扰：{existing}")

    output.mkdir(parents=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.FileHandler(output / "extraction.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    copied = output / drawing.name
    original_hash = sha256(drawing)
    shutil.copy2(drawing, copied)
    manifest = {
        "project_root": str(project),
        "source_dwg": str(drawing),
        "source_sha256": original_hash,
        "copied_dwg": str(copied),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }
    manifest_path = output / "extraction_manifest.json"
    acad = None
    owned_hwnd = None
    owned_pid = None
    pythoncom.CoInitialize()
    try:
        with allow_direct_cad():
            acad = connect_autocad(force_visible=False, prefer_new=True)
            instance = describe_autocad_instance(acad)
            owned_hwnd, owned_pid = instance.get("hwnd"), instance.get("pid")
            if not owned_hwnd or not owned_pid:
                raise RuntimeError("无法核实 AutoCAD 实例归属，停止提取")
            manifest["autocad_instance"] = instance
            result = extract_single_dwg(
                acad, str(copied), str(output), raise_on_error=True
            )
        extracted = copied.with_suffix(".json")
        data = json.loads(extracted.read_text(encoding="utf-8-sig"))
        if result != "success" or not data.get("entities"):
            raise RuntimeError("提取结果无有效实体")
        if sha256(drawing) != original_hash:
            raise RuntimeError("原始图纸哈希发生变化")
        manifest.update({
            "status": "completed",
            "json_path": str(extracted),
            "json_sha256": sha256(extracted),
            "entity_count": len(data["entities"]),
            "source_unchanged": True,
            "diagnostics": data.get("extraction_diagnostics"),
        })
    except Exception as exc:
        manifest.update(status="failed", error=str(exc))
        logging.exception("试点 DWG 提取失败")
        raise
    finally:
        if acad is not None and owned_pid and psutil.pid_exists(owned_pid):
            manifest["autocad_released"] = close_autocad_after_dwg(
                acad, "设计意图试点", force=True, expected_hwnd=owned_hwnd
            )
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        pythoncom.CoUninitialize()
    print(f"提取完成：{manifest_path}")


if __name__ == "__main__":
    main()
