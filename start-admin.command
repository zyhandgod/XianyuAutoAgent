#!/bin/zsh
cd -- "${0:A:h}"
exec .venv/bin/python -u admin.py
