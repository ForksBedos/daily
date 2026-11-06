#!/usr/bin/env bash
# Проверка границы безопасности `block-dangerous-commands.sh`.
#
# Зачем файлом, а не строкой в чате: сами проверочные команды содержат опасные образцы, и хук
# блокирует тот вызов Bash, внутри которого они написаны. Единственный способ прогнать их —
# собрать строки в файле и запустить файл.
#
# Запуск: bash .claude/hooks/tests/test_block_dangerous_commands.sh

set -u

HOOK="$(cd "$(dirname "$0")/.." && pwd)/block-dangerous-commands.sh"
PASS=0
FAIL=0

# Как и в хуке: `python3` на Windows бывает заглушкой Microsoft Store.
PYTHON_BIN=
for candidate in python3 python py; do
  if "$candidate" -c 'import sys' >/dev/null 2>&1; then
    PYTHON_BIN=$candidate
    break
  fi
done
[ -n "$PYTHON_BIN" ] || { echo "Python не найден"; exit 1; }

# Ожидание: allow | block
check() {
  local expected="$1" command="$2" payload actual
  payload=$(COMMAND="$command" "$PYTHON_BIN" -c '
import json, os
print(json.dumps({"tool_input": {"command": os.environ["COMMAND"]}}))')
  if printf "%s" "$payload" | bash "$HOOK" >/dev/null 2>&1; then
    actual="allow"
  else
    actual="block"
  fi
  if [ "$actual" = "$expected" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf 'НЕ СОШЛОСЬ: ждали %s, получили %s — %s\n' "$expected" "$actual" "$command"
  fi
}

# ── Обычные команды Git хук не останавливает: запрет на Git в проекте — правило, а не хук ──
check allow "git push origin master"
check allow "git push"
check allow "git push origin HEAD:master"   # src:dst — не удаление
check allow "git status"
check allow "git commit -m 'fix'"

# ── Перезапись истории на сервере — закрыта, флаг в любой позиции ───────────────────
FORCE="--for""ce"
check block "git push $FORCE origin master"
check block "git push origin master $FORCE"
check block "git push -f origin master"
check block "git push ${FORCE}-with-lease origin master"

# ── Те же последствия БЕЗ слова force: refspec с плюсом и удаление ветки ────────────
check block "git push origin +master"
check block "git push --delete origin feature"
check block "git push origin :feature"

# ── Остальная граница не должна была пострадать ────────────────────────────────────
check block "git reset --hard origin/master"
check block "git clean -fd"
check block "docker volume rm some_volume"
check block "drop database uzdigest"

# ── docker compose down с томами стирает именованные тома ──
check block "docker compose -p probe down -v"
check block "docker compose down --volumes"
check block "docker-compose -f docker-compose.prod.yml down -v --remove-orphans"
check block "docker compose down -tv 5"
check block "docker compose down -v=true"
check allow "docker compose down"
check allow "docker compose logs -v web"
check allow "docker compose down --remove-orphans"
check allow "docker compose up -d web"

# ── глобальные флаги docker перед compose не должны обходить блок ──
check block "docker --context desktop-linux compose down -v"
check block "docker --host unix:///var/run/docker.sock compose down --volumes"
check block "docker -H tcp://127.0.0.1:2375 --log-level debug compose -p probe down -v"
check allow "docker --context desktop-linux compose down"
check allow "docker --context desktop-linux compose logs -v web"
check allow "docker --context desktop-linux ps -a"

# ── хвост ключа: ; && | кавычка не обходят блок ──
check block "docker compose down -v; echo ok"
check block "docker compose down -v&&echo ok"
check block "docker compose down -v|tee log"
check block "bash -c \"docker compose down -v\""
check block "git push origin master -f;echo ok"
check block "git clean -fd;ls"
check block "git branch -d old;ls"
check allow "docker compose down; ls -v"
# ранний compose/down в той же строке не прячет последующий запрещённый down с томами
check block "docker compose ps x docker compose down -v"
check block "docker compose --project-directory /srv/down down -v"
check block "docker compose down && docker compose down -v"
check allow "git push origin master;echo -f"

# ── база UzDigest: таблицы и файлы базы, копии базы ──
check block "sqlite3 data/uzdigest.sqlite3 'drop table items'"
check block "sqlite3 data/uzdigest.sqlite3 \"DROP TABLE IF EXISTS runs\""
check block "rm data/uzdigest.sqlite3"
check block "rm -f data/uzdigest.sqlite3-wal"
check block "del data\uzdigest.sqlite3"
check block "Remove-Item data\uzdigest.sqlite3"
check block "Remove-Item -Path data/backup/uzdigest-2026-11-04.sqlite3"
check block "rm data/backup/uzdigest-before-008.sqlite3"
check allow "cp data/uzdigest.sqlite3 data/backup/uzdigest-before-009.sqlite3"
check allow "ls -la data/uzdigest.sqlite3"
check allow "rm data/dedup_pairs.csv"

# ── секреты: .env не читаем ни одной командой, образец .env.example можно ──
check block "cat .env"
check block "cat ./.env"
check block "head -5 .env"
check block "grep TOKEN .env"
check block "type .env"
check block "Get-Content .env"
check block "gc .\.env"
check block "Select-String -Path .env -Pattern TOKEN"
check block "sed -n 1,5p .env"
check block "cp .env /tmp/x"
check block "cat C:/Users/m.kachurova/Desktop/proj/daily/.env"
check block "cat secrets/token.txt"
check allow "cat .env.example"
check allow "ls -la .env"
check allow "Test-Path .env"
check allow "cat .venv/pyvenv.cfg"

# ── скорость: патологическая строка без ;&| проверяется быстро ──
LONG=$(for _ in $(seq 1 200); do printf 'docker com''pose do''wn x '; done)
T0=$(date +%s%N)
check allow "$LONG"
T1=$(date +%s%N)
MS=$(( (T1 - T0) / 1000000 ))
if [ "$MS" -lt 2000 ]; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
  printf 'НЕ СОШЛОСЬ: длинная строка проверялась %s мс (порог 2000)\n' "$MS"
fi

printf '\nПройдено: %d, провалено: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
