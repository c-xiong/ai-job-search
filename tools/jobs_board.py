#!/usr/bin/env python3
"""A local triage board for scraped jobs. Dense table, keyboard-driven, no dependencies.

    python3 tools/jobs_board.py            # opens http://127.0.0.1:8765/?t=<token>
    python3 tools/jobs_board.py --port 9000 --no-open

This file is the entry point people already type. The implementation lives in
`tools/board/` - see that package's docstring for the split.

It moved because the server had outgrown one file, and because the page is now a
set of files under `tools/board/static/` rather than a constant baked in at
import time: editing the page and refreshing the browser now does what it looks
like it does, which is why the old "this page is from an older build" banner is
gone rather than merely quieter.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from board.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
