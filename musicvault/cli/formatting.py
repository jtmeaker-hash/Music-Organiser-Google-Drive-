"""
Responsive terminal formatting (mobile narrow cards vs desktop wide tables),
Drive file ID redaction/truncation, and UTF-8 / formula-injection-safe CSV & JSON exports.
"""
import csv
import json
import os
import shutil
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config import redact_secrets

_FORMULA_PREFIX_CHARS = ("=", "+", "-", "@", "\t", "\r")


def get_terminal_width(override_width: Optional[int] = None) -> int:
    """Detect current terminal width safely for mobile Termux vs desktop layouts."""
    if override_width is not None and override_width > 0:
        return int(override_width)
    env_cols = os.environ.get("MUSICVAULT_COLUMNS", "").strip()
    if env_cols.isdigit() and int(env_cols) > 0:
        return int(env_cols)
    return shutil.get_terminal_size((80, 24)).columns


def short_file_id(file_id: Optional[str], verbose: bool = False) -> str:
    """Truncate Drive file ID by default on terminal output unless --verbose is requested."""
    fid = str(file_id or "").strip()
    if not fid:
        return "-"
    if verbose or len(fid) <= 12:
        return fid
    return f"{fid[:5]}...{fid[-4:]}"


def format_mib(num_bytes: int) -> str:
    """Format byte count concisely in KiB or MiB."""
    b = max(0, int(num_bytes))
    if b < 1024 * 1024:
        return f"{b / 1024.0:.1f} KiB ({b} B)"
    return f"{b / (1024.0 * 1024.0):.2f} MiB ({b} B)"


def sanitize_csv_cell(value: Any, extra_secrets: Iterable[Optional[str]] = ()) -> str:
    """
    Sanitize a cell value for safe spreadsheet CSV opening:
    1. Redacts any secret patterns or configured credentials.
    2. Neutralizes CSV formula injection (=, +, -, @, tab, CR, including after leading spaces)
       by prefixing a single quote, while preserving plain negative numbers if needed (here track/text fields are guarded).
    """
    if value is None:
        return ""
    clean = redact_secrets(value, extra_secrets)
    if not clean:
        return ""
    stripped_leading = clean.lstrip(" ")
    if clean.startswith(_FORMULA_PREFIX_CHARS) or (
        stripped_leading and stripped_leading.startswith(_FORMULA_PREFIX_CHARS)
    ):
        return "'" + clean
    return clean


def export_safe_csv(
    dest_path: Path,
    fieldnames: Sequence[str],
    rows: Sequence[Dict[str, Any]],
    extra_secrets: Iterable[Optional[str]] = (),
) -> Path:
    """Export UTF-8 CSV report with formula-injection protection and secret redaction."""
    dest_path = Path(dest_path).expanduser()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_list = list(extra_secrets)
    with dest_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            safe_row = {
                k: sanitize_csv_cell(r.get(k, ""), secrets_list)
                for k in fieldnames
            }
            writer.writerow(safe_row)
    return dest_path


def _redact_json_tree(obj: Any, secrets_list: List[Optional[str]]) -> Any:
    if isinstance(obj, dict):
        return {str(k): _redact_json_tree(v, secrets_list) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_json_tree(v, secrets_list) for v in obj]
    if isinstance(obj, str):
        return redact_secrets(obj, secrets_list)
    return obj


def export_safe_json(
    dest_path: Path,
    payload: Any,
    extra_secrets: Iterable[Optional[str]] = (),
) -> Path:
    """Export UTF-8 JSON report with recursive secret redaction."""
    dest_path = Path(dest_path).expanduser()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    clean_payload = _redact_json_tree(payload, list(extra_secrets))
    dest_path.write_text(
        json.dumps(clean_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest_path


def render_responsive_records(
    records: Sequence[Dict[str, Any]],
    columns: Sequence[tuple[str, str, int]],
    force_wide: bool = False,
    term_width: Optional[int] = None,
) -> str:
    """
    Render records as vertical wrapped key/value cards on narrow mobile screens (< 100 cols)
    or as an aligned horizontal table on wide desktop terminals (>= 100 cols or --wide).
    `columns` is a sequence of (key, header_label, wide_col_width).
    """
    if not records:
        return "  (no records to display)"

    width = get_terminal_width(term_width)
    use_wide = force_wide or width >= 105

    if use_wide:
        header_line = " ".join(f"{label:<{col_w}}" for _, label, col_w in columns)
        sep_line = "-" * min(max(len(header_line), 40), max(width, len(header_line)))
        out_lines = [header_line, sep_line]
        for rec in records:
            cells = []
            for key, _, col_w in columns:
                val = str(rec.get(key, "") or "")
                if len(val) > col_w - 1 and col_w > 4:
                    val = val[: col_w - 2] + "…"
                cells.append(f"{val:<{col_w}}")
            out_lines.append(" ".join(cells).rstrip())
        return "\n".join(out_lines)

    # Narrow mobile card layout (wraps cleanly on 45-85 col phone screens)
    card_w = max(36, min(width - 2, 88))
    sep = "-" * card_w
    cards: List[str] = []
    for idx, rec in enumerate(records, 1):
        lines = [sep]
        for key, label, _ in columns:
            val = str(rec.get(key, "") or "")
            if not val and key not in ("destination", "reason", "status"):
                continue
            prefix = f"  {label}: "
            wrapped = textwrap.fill(
                val or "-",
                width=card_w,
                initial_indent=prefix,
                subsequent_indent=" " * len(prefix),
            )
            lines.append(wrapped)
        cards.append("\n".join(lines))
    cards.append(sep)
    return "\n".join(cards)
