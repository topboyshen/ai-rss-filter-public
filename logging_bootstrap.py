"""Configure API startup logging before importing core or route dependencies."""

import logging
import sys

# Keep early module INFO logs on stderr, where Gunicorn captures them. Exporting
# the configured logging module lets app.py retain normal top-level imports.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)
