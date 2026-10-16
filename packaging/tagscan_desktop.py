"""PyInstaller entry point."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from tagscan.cli import main
    sys.exit(main())
