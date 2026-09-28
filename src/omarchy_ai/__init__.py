"""Omarchy AI — an independent voice assistant for Omarchy."""

__all__ = ["main"]
__version__ = "0.1.0"


def main():
    # Lazy: importing the daemon loads the wake-word models and scikit-learn
    # (1.8 s). Every CLI and user tool that imports anything from this package
    # paid that (2026-09-28: `omarchy-ai-vault get` took 2.7 s, 1.8 s of it here).
    from .core.daemon import main as daemon_main
    return daemon_main()
