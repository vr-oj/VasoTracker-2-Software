#!/usr/bin/env python3
"""
Verify that recorded TIFF stacks and CSV traces stayed in sync.

By default the newest ``*_trace.csv`` within ``--root`` (current directory)
is validated. Specify ``--trace`` to validate a particular file.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import tifffile


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate that saved TIFF pages and CSV rows match 1:1."
    )
    parser.add_argument(
        "--trace",
        type=Path,
        help="Path to the *_trace.csv file to validate.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Root directory to search when --trace is omitted (default: current working directory).",
    )
    return parser.parse_args()


def _select_trace(trace: Optional[Path], root: Path) -> Optional[Path]:
    if trace:
        return trace.resolve()

    candidates = sorted(
        root.rglob("*_trace.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return candidates[0]


def _load_saved_rows(trace_path: Path) -> List[Dict[str, int]]:
    saved_rows: List[Dict[str, int]] = []
    with trace_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{trace_path} has no header row.")
        for index, row in enumerate(reader, start=2):
            saved_flag = (row.get("Saved") or "").strip().lower()
            if saved_flag in {"1", "true", "yes"}:
                try:
                    page_val = int((row.get("TiffPage") or "").strip())
                except ValueError as exc:
                    raise ValueError(
                        f"{trace_path}: row {index} has invalid TiffPage value {row.get('TiffPage')!r}"
                    ) from exc
                try:
                    frame_val = int((row.get("FrameNumber") or "").strip())
                except ValueError as exc:
                    raise ValueError(
                        f"{trace_path}: row {index} has invalid FrameNumber value {row.get('FrameNumber')!r}"
                    ) from exc
                saved_rows.append(
                    {
                        "index": index,
                        "page": page_val,
                        "frame": frame_val,
                    }
                )
    return saved_rows


def _load_tiff_metadata(tiff_path: Path) -> Dict[int, Dict[str, int]]:
    metadata_by_page: Dict[int, Dict[str, int]] = {}
    with tifffile.TiffFile(tiff_path) as tif:
        for stack_index, page in enumerate(tif.pages):
            description = getattr(page, "description", "") or ""
            payload: Dict[str, int]
            try:
                payload = json.loads(description) if description else {}
            except json.JSONDecodeError:
                payload = {}
            page_id = int(payload.get("TiffPage", stack_index))
            frame_id = payload.get("FrameNumber", stack_index)
            metadata_by_page[page_id] = {
                "frame": int(frame_id),
                "stack_index": stack_index,
            }
    return metadata_by_page


def _validate_pair(trace_path: Path) -> None:
    saved_rows = _load_saved_rows(trace_path)
    saved_count = len(saved_rows)

    raw_tiff = trace_path.with_name(f"{trace_path.stem}_Raw.tiff")
    result_tiff = trace_path.with_name(f"{trace_path.stem}_Result.tiff")

    raw_exists = raw_tiff.exists()
    result_exists = result_tiff.exists()

    if saved_count == 0:
        if raw_exists or result_exists:
            raise RuntimeError(
                f"{trace_path.name}: found TIFF stacks but CSV contains no saved rows."
            )
        print(f"OK: {trace_path.name} has no saved rows; nothing to check.")
        return

    if not raw_exists or not result_exists:
        missing = [
            name
            for name, exists in (
                (raw_tiff.name, raw_exists),
                (result_tiff.name, result_exists),
            )
            if not exists
        ]
        raise FileNotFoundError(
            f"{trace_path.name}: missing TIFF stack(s): {', '.join(missing)}"
        )

    raw_meta = _load_tiff_metadata(raw_tiff)
    result_meta = _load_tiff_metadata(result_tiff)

    if len(raw_meta) != len(result_meta):
        raise RuntimeError(
            f"{trace_path.name}: Raw ({len(raw_meta)}) and Result ({len(result_meta)}) page counts differ."
        )

    if len(raw_meta) != saved_count:
        raise RuntimeError(
            f"{trace_path.name}: {saved_count} saved CSV rows but {len(raw_meta)} TIFF pages."
        )

    for row in saved_rows:
        page_id = row["page"]
        frame_id = row["frame"]
        if page_id not in raw_meta:
            raise RuntimeError(
                f"{trace_path.name}: row {row['index']} references missing TIFF page {page_id}."
            )
        raw_frame = raw_meta[page_id]["frame"]
        result_frame = result_meta[page_id]["frame"]
        if raw_frame != frame_id or result_frame != frame_id:
            raise RuntimeError(
                f"{trace_path.name}: row {row['index']} expected frame {frame_id}, "
                f"but TIFF metadata reports raw={raw_frame}, result={result_frame}."
            )

    print(
        f"OK: {trace_path.name} -> {saved_count} saved rows match TIFF pages 1:1."
    )


def main() -> None:
    args = _parse_args()
    trace_path = _select_trace(args.trace, args.root)
    if trace_path is None:
        print(f"OK: No *_trace.csv files found under {args.root}.")
        return
    if not trace_path.exists():
        raise FileNotFoundError(f"Trace file does not exist: {trace_path}")
    _validate_pair(trace_path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - simple CLI script
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
