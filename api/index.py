"""Vercel serverless entry point for the FastAPI GrantWatch UI."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

# Ensure project modules are importable when running inside Vercel.
for path in (ROOT_DIR, SRC_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

try:
    from web.app import app as fastapi_app
except ModuleNotFoundError as exc:  # pragma: no cover - aids runtime diagnostics on Vercel
    raise RuntimeError("Unable to import FastAPI app from src/web/app.py") from exc

app = fastapi_app
