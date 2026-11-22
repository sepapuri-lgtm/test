"""PyInstaller 진입점. 인자 없이 실행하면 창을 연다."""
import sys

from receipt_renamer.cli import main

if __name__ == "__main__":
    sys.exit(main())
