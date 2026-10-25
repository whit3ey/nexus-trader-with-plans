#!/usr/bin/env python3
"""Prints the names of required Python packages that are NOT installed (stdlib only, Python 3.8+).
Exit code 0 = everything present, 1 = something missing."""
import importlib
import sys

REQUIRED = [("yaml", "PyYAML"), ("dotenv", "python-dotenv"), ("pybit", "pybit")]
missing = []
for mod, pkg in REQUIRED:
    try:
        importlib.import_module(mod)
    except Exception:
        missing.append(pkg)
print(" ".join(missing))
sys.exit(1 if missing else 0)
