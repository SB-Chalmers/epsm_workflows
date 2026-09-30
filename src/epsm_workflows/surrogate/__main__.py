"""Compatibility entrypoint for the unified workflow CLI."""

from ..cli import main

if __name__ == "__main__":
    raise SystemExit(main())
