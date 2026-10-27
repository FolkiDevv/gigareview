"""Запуск ``python -m gigareview`` и консольной команды ``gigareview``."""

import sys

from gigareview.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
