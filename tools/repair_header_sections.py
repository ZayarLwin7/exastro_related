#!/usr/bin/env python3
"""Report -- and optionally undo -- header sections an <input> flattened.

    python tools/repair_header_sections.py            # report only
    python tools/repair_header_sections.py -w         # write the repairs

Header Section was once edited in a single-line <input type="text">. The HTML
input value sanitization algorithm strips line breaks, so every save through
that field stored the section with its newlines deleted and its indentation
left behind -- which is why a flattened section reads as `localhost  remote_user`
with two spaces rather than one.

The textarea that replaced it stopped the bleeding, but it cannot undo what was
already stored, and a stored squashed section is refused on save: renaming the
profile resubmits the whole form, so a defect in one field blocked every
unrelated edit.

A squashed section has lost the characters that said where its lines began, so
this repairs only what can be *proved*: a section that reproduces exactly the
install default, or exactly another profile's unbroken section, with the line
breaks removed. Everything else is reported and left alone. Inventing line
breaks in YAML produces a header that parses and means something else.

The file is copied beside itself before anything is written.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import settings                  # noqa: E402


def _header_of(raw: str) -> str:
    try:
        return (json.loads(raw or "{}") or {}).get("HEADER_SECTION") or ""
    except ValueError:
        return ""


def main() -> int:
    write = "-w" in sys.argv[1:]
    db = settings.SETTINGS_DB
    if not pathlib.Path(db).exists():
        print(f"no settings store at {db}")
        return 2
    conn = settings._connect()
    rows = conn.execute("SELECT id, name, payload FROM profiles ORDER BY id"
                        ).fetchall()
    broken = []
    for row_id, name, raw in rows:
        stored = _header_of(raw)
        if stored and not settings.header_section_is_block_list(stored):
            broken.append((row_id, name, stored))
    if not broken:
        print(f"every header section in {db} keeps its line breaks")
        return 0

    repairs = []
    for row_id, name, stored in broken:
        fixed = settings.repair_flattened_header_section(stored, row_id)
        if fixed:
            repairs.append((row_id, name, stored, fixed))
        else:
            print(f"profile {row_id} ({name}): flattened, and no unbroken "
                  f"original is on hand. Retype it in the form:\n    {stored}")
    if not repairs:
        print("\nnothing could be repaired without guessing")
        return 1

    print()
    for row_id, name, was, now in repairs:
        print(f"profile {row_id} ({name}):")
        print(f"  was  {was!r}")
        print(f"  now  {now!r}")
    if not write:
        print("\nreport only. Re-run with -w to store these.")
        return 0

    backup = f"{db}.backup-header-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(db, backup)
    with conn:
        for row_id, _name, _was, fixed in repairs:
            raw = conn.execute("SELECT payload FROM profiles WHERE id=?",
                               (row_id,)).fetchone()[0]
            payload = json.loads(raw or "{}")
            payload["HEADER_SECTION"] = fixed
            conn.execute("UPDATE profiles SET payload=? WHERE id=?",
                         (json.dumps(payload, ensure_ascii=False), row_id))
    print(f"\nrepaired {len(repairs)} profile(s); {db} copied to {backup}")
    print("restart the service so the cached per-user clients pick this up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())