#!/usr/bin/env python3
"""Compare committed and generated federation entry contracts.

The federation plugin can assign different local variable names on Windows and
Linux. Those names do not affect runtime behavior, so compare the stable public
contract and asset references instead of requiring byte-identical output.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REMOTE_ENTRY = Path("plugins.v2/embytmdbcollectionsync/dist/assets/remoteEntry.js")
STRING_LITERAL = re.compile(r"(['\"])(.*?)\1")
PUBLIC_EXPORT = re.compile(r"\bas\s+([A-Za-z_$][\w$]*)")


@dataclass(frozen=True)
class RemoteContract:
    exposes: frozenset[str]
    assets: frozenset[str]
    exports: frozenset[str]


def extract_contract(content: str) -> RemoteContract:
    """Extract stable module federation strings from a generated entry."""
    strings = [match.group(2) for match in STRING_LITERAL.finditer(content)]
    exposes = frozenset(
        value for value in strings if value.startswith("./") and "." not in value[2:]
    )
    assets = frozenset(
        value.removeprefix("./")
        for value in strings
        if value.endswith((".js", ".css"))
    )
    exports = frozenset(PUBLIC_EXPORT.findall(content))
    return RemoteContract(exposes=exposes, assets=assets, exports=exports)


def committed_content(path: Path) -> str:
    """Read the committed entry without modifying the working tree."""
    result = subprocess.run(
        ["git", "show", f"HEAD:{path.as_posix()}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def validate_assets(path: Path, assets: frozenset[str]) -> list[str]:
    """Ensure every local asset referenced by the generated entry exists."""
    errors: list[str] = []
    for asset in sorted(assets):
        if not (path.parent / asset).is_file():
            errors.append(f"generated remote entry references missing asset: {asset}")
    return errors


def main() -> int:
    if not REMOTE_ENTRY.is_file():
        print(f"Remote entry validation failed: missing {REMOTE_ENTRY}")
        return 1

    committed = extract_contract(committed_content(REMOTE_ENTRY))
    generated = extract_contract(REMOTE_ENTRY.read_text(encoding="utf-8"))
    errors: list[str] = []

    if committed != generated:
        for field in ("exposes", "assets", "exports"):
            old = getattr(committed, field)
            new = getattr(generated, field)
            if old != new:
                errors.append(
                    f"remote entry {field} changed: committed={sorted(old)!r}, "
                    f"generated={sorted(new)!r}"
                )

    required_exports = {"dynamicLoadingCss", "get", "init"}
    if not required_exports.issubset(generated.exports):
        errors.append(
            "generated remote entry is missing public exports: "
            f"{sorted(required_exports - generated.exports)!r}"
        )

    errors.extend(validate_assets(REMOTE_ENTRY, generated.assets))
    if errors:
        print("Remote entry validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "Remote entry validation passed: "
        f"{len(generated.exposes)} exposes, {len(generated.assets)} assets, "
        f"{len(generated.exports)} public exports."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
