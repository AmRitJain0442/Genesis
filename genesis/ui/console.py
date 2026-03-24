import sys
from rich.console import Console

# Force UTF-8 on Windows so special characters render correctly.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# Shared console singleton — import this everywhere instead of creating new instances
console = Console()
