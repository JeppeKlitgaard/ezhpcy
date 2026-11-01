"""Says what `getquota_zhome.sh` says, tersely: `19.4/30.0G [18:04]`."""

import re
import subprocess
import sys

_USAGE = re.compile(r"using ([\d.]+)(\w+) of ([\d.]+)(\w+) quota")
_UPDATED = re.compile(r"Last Update: \S+ (\d\d:\d\d)")


def terse(output: str) -> str | None:
    """`getquota_zhome.sh`'s output in a few characters, or None if it can't be read."""
    usage = _USAGE.search(output)
    updated = _UPDATED.search(output)
    if usage is None or updated is None:
        return None
    used, used_unit, total, total_unit = usage.groups()
    # Its unit only if it differs from the total's.
    shown_unit = "" if used_unit == total_unit else used_unit
    return (
        f"{float(used):.1f}{shown_unit}/{float(total):.1f}{total_unit} [{updated[1]}]"
    )


def main() -> int:
    # On DTU HPC's PATH only in a login shell.
    result = subprocess.run(
        ["bash", "-lc", "getquota_zhome.sh"],
        capture_output=True,
        text=True,
        check=False,
    )
    summary = terse(result.stdout)
    if result.returncode != 0 or summary is None:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode or 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
