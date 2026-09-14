#!/usr/bin/env python3
"""Report every tracked file that still contains a name from a deny list.

    python tools/scrub_check.py .scrub-denylist

One identifier per line; blank lines and `#` comments are ignored. The list is
deliberately not part of the repository -- it is the thing being kept out -- so
the local path is git-ignored and the file is never committed.

This exists because the first sweep was built from the diff of what had been
pushed, and a diff of one push is not the list of names on the install. The
inventory behind a running platform is: movements, sheets, role variables,
packages, execution environments, and the account name. Anything that can be
read back from it is a candidate, which is what the caller should paste in.

Exit status: 0 clean, 1 names found, 2 the list could not be read.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

SKIP_SUFFIXES = (".db", ".png", ".ico", ".woff", ".woff2", ".pyc")


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("not a git checkout?")
    return [f for f in out.stdout.split()
            if ".venv" not in f and not f.endswith(SKIP_SUFFIXES)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    path = pathlib.Path(argv[1])
    try:
        names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
    except OSError as exc:
        print(f"cannot read the deny list: {exc}", file=sys.stderr)
        return 2
    files = tracked_files()
    bodies = {}
    for f in files:
        try:
            bodies[f] = pathlib.Path(f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    hits: dict[str, list[str]] = {}
    for name in sorted(set(names)):
        for f, body in bodies.items():
            if name in body:
                hits.setdefault(name, []).append(f)
    print(f"{len(set(names))} names checked against {len(bodies)} files")
    if not hits:
        print("clean")
        return 0
    print(f"{len(hits)} name(s) still present:")
    for name, where in sorted(hits.items()):
        print(f"  {name}  ->  {', '.join(sorted(where))}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
