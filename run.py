"""CareerDesk source-checkout launcher."""

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
os.environ.setdefault("CAREERDESK_RESOURCE_ROOT", str(ROOT))
os.environ.setdefault("CAREERDESK_CONFIG_FILE", str(ROOT / ".env"))
os.environ.setdefault("APP_RUNTIME_MODE", "desktop")


def main() -> int:
    """Delegate to the same launcher shipped by the installed backend package."""
    from careerdesk.bootstrap.desktop import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
