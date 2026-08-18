from __future__ import annotations

import os
import sys
from pathlib import Path


def run() -> None:
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    from streamlit.web import cli as streamlit_cli

    app_path = Path(__file__).with_name("app.py")
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.address=0.0.0.0",
        "--server.port=8501",
        "--browser.gatherUsageStats=false",
    ]
    raise SystemExit(streamlit_cli.main())
