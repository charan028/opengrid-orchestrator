"""Import roots for the cross-system PQ suite (TS-15b).

This suite is the one place that imports BOTH `ogsim` and `opengrid` (BUILD.md S1 forbids it in either
product's own code; `orchestrator/tools/dupcheck.py` scans only `orchestrator/src` and
`integration-sims/src`). CI installs both packages editable; putting this checkout's `src/` roots first
also makes a plain local run exercise the code in this working tree rather than another install:

    python -m pytest tests-e2e/pq -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _src in (_REPO_ROOT / "integration-sims" / "src", _REPO_ROOT / "orchestrator" / "src"):
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
