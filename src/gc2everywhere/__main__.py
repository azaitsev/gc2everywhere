"""Allow python -m gc2everywhere as well as the installed gc2e command."""

import sys

from .cli import main

sys.exit(main())
