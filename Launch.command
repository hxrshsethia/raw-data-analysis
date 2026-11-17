#!/bin/zsh
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print 'Set up the local Python environment using the instructions in README.md.'
  exit 1
fi
exec .venv/bin/python app.py
