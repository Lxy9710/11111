"""Probe whether AutoCAD COM is usable on this machine.

The FAXIN corpus was extracted from DWG with AutoCAD, so the same extractor
can recover the 13 design drawings that have no JSON yet. Before wiring that
in, check that COM can actually reach an AutoCAD instance here: a licensed
install may still refuse automation, or may block on a dialog.

This only connects and reads version information. It opens no document.
"""

from __future__ import annotations

import sys
import time

import win32com.client
from win32com.client import pythoncom


def probe(dispatch_ex: bool = False) -> dict:
    result = {"mode": "DispatchEx" if dispatch_ex else "Dispatch", "ok": False}
    start = time.time()
    try:
        if dispatch_ex:
            acad = win32com.client.DispatchEx("AutoCAD.Application")
        else:
            acad = win32com.client.Dispatch("AutoCAD.Application")
    except Exception as exc:  # noqa: BLE001 - the point is to report anything
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["seconds"] = round(time.time() - start, 1)
        return result

    try:
        result["version"] = str(acad.Version)
        result["name"] = str(acad.Name)
        result["visible"] = bool(acad.Visible)
        try:
            result["documents"] = int(acad.Documents.Count)
        except Exception:
            result["documents"] = None
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"读取属性失败 {type(exc).__name__}: {exc}"
    finally:
        result["seconds"] = round(time.time() - start, 1)
        # Do not quit: the instance may be the user's own running AutoCAD.
        try:
            acad = None
        except Exception:
            pass
    return result


def main() -> int:
    pythoncom.CoInitialize()
    try:
        for dispatch_ex in (False, True):
            report = probe(dispatch_ex)
            print(f"[{report['mode']}]")
            for key, value in report.items():
                if key == "mode":
                    continue
                print(f"    {key}: {value}")
            if report["ok"]:
                print("  -> AutoCAD COM 可用")
                return 0
        print("  -> AutoCAD COM 不可用")
        return 1
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    sys.exit(main())
