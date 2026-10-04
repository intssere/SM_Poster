"""Backend-only module entrypoint; no application startup or repository imports."""
from .migration_cli import main


if __name__ == "__main__":
    raise SystemExit(main())