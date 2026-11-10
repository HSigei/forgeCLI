import pathlib
import sys

try:
    import prompt_toolkit  # noqa: F401
    import rich  # noqa: F401
except ImportError:
    req = pathlib.Path(__file__).resolve().parent / "requirements.txt"
    sys.exit(f"Forge needs two packages. Install them with:\n  python -m pip install -r \"{req}\"")

from forgecli.cli import main

if __name__ == "__main__":
    sys.exit(main())
