"""Allow running the CLI as ``python -m goosetown``."""

import sys

from goosetown.cli import main

if __name__ == "__main__":
    sys.exit(main())
