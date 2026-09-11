"""系统级接口：目录浏览、系统目录选择对话框。"""
from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, jsonify, request

bp = Blueprint("system", __name__, url_prefix="/api/system")


@bp.post("/select-folder")
def api_system_select_folder():
    payload = request.get_json(silent=True) or {}
    initial_dir = str(payload.get("initial_dir", "")).strip()

    if initial_dir:
        initial = Path(initial_dir).expanduser()
        initial_dir = str(initial.resolve()) if initial.exists() else ""

    try:
        if sys.platform.startswith("win"):
            selected = _select_folder_windows(initial_dir)
        else:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                initialdir=initial_dir or None,
                title="选择目录",
                mustexist=False,
            )
            root.destroy()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"系统目录选择失败: {exc}"}), 500

    chosen = str(Path(selected).resolve()) if selected else ""
    return jsonify({"ok": True, "path": chosen})


@bp.get("/directories")
def api_system_directories():
    raw_path = str(request.args.get("path", "")).strip()
    current = _normalize_existing_dir(raw_path)

    if current is None:
        roots = _list_directory_roots()
        return jsonify(
            {
                "ok": True,
                "is_root": True,
                "current": "",
                "parent": "",
                "items": [{"name": str(root), "path": str(root)} for root in roots],
            }
        )

    parent = current.parent
    parent_path = "" if parent == current else str(parent)

    children: list[dict[str, str]] = []
    try:
        dirs = [p for p in current.iterdir() if p.is_dir()]
    except (PermissionError, OSError):
        dirs = []

    dirs.sort(key=lambda p: p.name.lower())
    for child in dirs:
        children.append({"name": child.name, "path": str(child)})

    return jsonify(
        {
            "ok": True,
            "is_root": False,
            "current": str(current),
            "parent": parent_path,
            "items": children,
        }
    )


def _select_folder_windows(initial_dir: str) -> str:
    ps_exe = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_exe:
        raise RuntimeError("未找到 PowerShell，无法打开系统目录选择器")

    escaped = initial_dir.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dlg.Description = '选择目录'; "
        "$dlg.ShowNewFolderButton = $true; "
        f"if ('{escaped}' -and (Test-Path -LiteralPath '{escaped}')) {{ $dlg.SelectedPath = '{escaped}' }}; "
        "$result = $dlg.ShowDialog(); "
        "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Write-Output $dlg.SelectedPath }"
    )

    proc = subprocess.run(  # noqa: S603
        [ps_exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"PowerShell 退出码 {proc.returncode}")

    return (proc.stdout or "").strip()


def _normalize_existing_dir(raw_path: str) -> Path | None:
    if not raw_path:
        return None

    raw = raw_path.strip()
    if not raw:
        return None

    if sys.platform.startswith("win") and re.fullmatch(r"[a-zA-Z]:", raw):
        raw = raw + "\\"

    path = Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return None

    if not resolved.exists() or not resolved.is_dir():
        return None
    return resolved


def _list_directory_roots() -> list[Path]:
    if sys.platform.startswith("win"):
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        roots: list[Path] = []
        for idx in range(26):
            if not (bitmask & (1 << idx)):
                continue
            letter = chr(ord("A") + idx)
            root = Path(f"{letter}:\\")
            if root.exists():
                roots.append(root)
        roots.sort(key=lambda p: str(p).lower())
        return roots
    return [Path("/")]
