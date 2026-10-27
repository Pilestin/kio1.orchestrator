"""Make `kio_mocks` importable however pytest is started.

The folder sits next to KIO1's code but is not part of it: nothing in KIO1
imports it, and it imports nothing from KIO1. So it is put on the path here
rather than installed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
