"""Omarchy AI — an independent voice assistant for Omarchy."""

__all__ = ["main"]
__version__ = "0.1.0"


def main():
    import sys
    if sys.argv[1:2] == ["doctor"]:  # `omarchy-ai doctor ...` (cli/doctor.py)
        from .cli.doctor import main as doctor_main
        return sys.exit(doctor_main(sys.argv[2:]))
    # Lazy: importing the daemon loads the wake-word models and scikit-learn
    # (1.8 s). Every CLI and user tool that imports anything from this package
    # paid that (2026-09-28: `omarchy-ai-vault get` took 2.7 s, 1.8 s of it here).
    from .core.daemon import main as daemon_main
    return daemon_main()
