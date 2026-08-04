"""Dispatch the two executables embedded in the self-contained bundle."""

import os
from pathlib import Path
import sys


def main() -> int:
    if os.environ.get("CAREERDESK_PACKAGE_SELF_TEST") == "1":
        from careerdesk.bootstrap.package_self_test import run_package_self_test

        run_package_self_test()
        return 0
    if Path(sys.executable).stem.casefold() == "careerdesk-data":
        from careerdesk.bootstrap.cli import main as cli_main

        return cli_main()
    from careerdesk.bootstrap.desktop import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
