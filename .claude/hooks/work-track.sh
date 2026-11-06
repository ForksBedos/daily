#!/usr/bin/env bash
# Запускает work_track.py. Этот хук только наблюдает: без Python работа идёт дальше, поэтому всегда exit 0.

cd "$(dirname "$0")" || exit 0

# На Windows `python3` бывает заглушкой Microsoft Store, поэтому проверяем, что интерпретатор работает.
for candidate in python3 python py; do
  if "$candidate" -c 'import sys' >/dev/null 2>&1; then
    "$candidate" work_track.py
    exit 0
  fi
done
exit 0
