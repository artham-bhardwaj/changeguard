"""Backwards-compatible entry point: the demo is now the full ChangeGuard web app."""
from changeguard.webapp import WebAppHandler as DemoHandler, main

__all__ = ["DemoHandler", "main"]

if __name__ == "__main__":
    main()
