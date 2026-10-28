"""fedtail-sync and fedtail are released together under one number: this server's
__version__ must be fedtail's.

    python3 tools/same_version.py ../fedtail/fedtail/__init__.py   # exit 1 when not
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def version_of(path):
    m = re.search(r'^__version__ = "([^"]+)"', Path(path).read_text("utf-8"), re.M)
    if not m:
        raise SystemExit(f"{path}: no __version__")
    return m.group(1)


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__)
    ours, theirs = version_of(HERE / "fedtail_sync.py"), version_of(argv[1])
    print(f"fedtail-sync {ours}, fedtail {theirs}")
    if ours != theirs:
        print("not the same: set fedtail_sync.__version__ to fedtail's", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
