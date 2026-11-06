#!/usr/bin/env bash
# PreToolUse security boundary.
# Exit 0 = allow. Exit 2 = deny with a safe, actionable reason on stderr.

INPUT=$(cat)

# On Windows `python3` may resolve to the Microsoft Store stub, so check that the interpreter runs.
PYTHON_BIN=
for candidate in python3 python py; do
  if "$candidate" -c 'import sys' >/dev/null 2>&1; then
    PYTHON_BIN=$candidate
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "BLOCKED [hook_runtime_unavailable]: Python is required to validate the command. Install Python or ask the user to run the command manually." >&2
  exit 2
fi

DECISION=$(
  printf '%s' "$INPUT" | "$PYTHON_BIN" -c '
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    print("BLOCK:invalid_json")
    raise SystemExit

tool_input = payload.get("tool_input")
if not isinstance(tool_input, dict):
    print("BLOCK:missing_tool_input")
    raise SystemExit

if "command" not in tool_input or not isinstance(tool_input["command"], str):
    print("BLOCK:missing_command")
    raise SystemExit

command = " ".join(tool_input["command"].split())
if not command:
    print("ALLOW")
    raise SystemExit

rules = (
    (
        "git_force_push",
        r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(?:--force(?:-with-lease|-if-includes)?\b|(?:^|\s)-f(?=[\s;&|\x22\x27]|$))",
    ),
    (
        # Перезапись и удаление ветки на сервере БЕЗ слова --force: refspec `+master`
        # переписывает историю так же, как форс, а `--delete`/`:branch` сносит ветку целиком.
        "git_push_destructive",
        r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(?:--delete\b|(?:^|\s)-d(?=[\s;&|\x22\x27]|$)|\s\+[A-Za-z0-9._/-]+|\s:[A-Za-z0-9._/-]+)",
    ),
    ("git_reset_hard", r"\bgit\b[^\n;&|]*\breset\b[^\n;&|]*--hard\b"),
    (
        "git_clean_force",
        r"\bgit\b[^\n;&|]*\bclean\b[^\n;&|]*(?:--force\b|(?:^|\s)-[a-z]*f[a-z]*(?=[\s;&|\x22\x27]|$))",
    ),
    (
        "git_branch_delete",
        r"\bgit\b[^\n;&|]*\bbranch\b[^\n;&|]*(?:--delete\b[^\n;&|]*--force\b|--force\b[^\n;&|]*--delete\b|(?:^|\s)-d(?=[\s;&|\x22\x27]|$))",
    ),
    (
        "broad_recursive_delete",
        r"\brm\b(?=[^\n;&|]*(?:--recursive\b|-[a-z]*r[a-z]*))(?=[^\n;&|]*(?:--force\b|-[a-z]*f[a-z]*))[^\n;&|]*\s+(?:/|/\*|~|~/\*|\.|\./\*|\.\.|\.\./\*)(?:\s|$|[;&|])",
    ),
    (
        "powershell_recursive_delete",
        r"\bremove-item\b(?=[^\n]*-recurse\b)(?=[^\n]*-force\b)",
    ),
    (
        "cmd_recursive_delete",
        r"(?:^|\s)(?:rmdir|rd)\b(?=[^\n]*/s\b)(?=[^\n]*/q\b)",
    ),
    ("docker_system_prune", r"\bdocker\s+system\s+prune\b"),
    ("docker_volume_remove", r"\bdocker\s+volume\s+(?:rm|remove)\b"),
    (
        # down с томами стирает и именованные тома с фиксированным name: -p их не изолирует
        "docker_compose_down_volumes",
        r"\bdocker(?:-compose\b|\b(?>[^\n;&|]*?\scompose\b))(?>[^\n;&|]*?\bdown\b)[^\n;&|]*?(?:--volumes\b|(?:^|\s)-[a-z]*v[a-z]*(?=[\s=;&|\x22\x27]|$))",
    ),
    ("drop_database", r"\bdrop\s+database\b"),
    ("drop_table", r"\bdrop\s+table\b"),
    ("truncate_table", r"\btruncate(?:\s+table)?\s+[^\s;]+"),
    (
        # Рабочая база и её копии: на них держится утренний дайджест, схема меняется только новым файлом
        "delete_database",
        r"(?:^|[\s;&|(])(?:rm|del|erase|ri|remove-item)\b[^\n;&|]*(?:uzdigest[\w.-]*\.sqlite3|data[/\\]+backup\b)",
    ),
    (
        # .env закрыт для Read в settings.json, а здесь закрыт для команд оболочки; .env.example можно
        "read_secrets",
        r"(?:^|[\s;&|(])(?:cat|bat|type|more|less|head|tail|sed|awk|grep|egrep|rg|findstr|select-string|gc|get-content|strings|od|xxd|hexdump|base64|cp|copy|copy-item|mv|move|move-item|scp|curl)\b[^\n;&|]*(?:(?<![\w.-])\.env(?![\w.-])|(?<![\w-])secrets[/\\])",
    ),
)

for reason, pattern in rules:
    if re.search(pattern, command, flags=re.IGNORECASE):
        print(f"BLOCK:{reason}")
        break
else:
    print("ALLOW")
'
)

case "$DECISION" in
  ALLOW)
    exit 0
    ;;
  BLOCK:git_force_push | BLOCK:git_push_destructive)
    MESSAGE="Git stays untouched until the owner says otherwise: see AGENTS.md."
    ;;
  BLOCK:git_reset_hard | BLOCK:git_clean_force | BLOCK:git_branch_delete)
    MESSAGE="Preserve the working tree and ask the user before changing or deleting Git state."
    ;;
  BLOCK:broad_recursive_delete | BLOCK:powershell_recursive_delete | BLOCK:cmd_recursive_delete)
    MESSAGE="Resolve an explicit narrow target and request user approval before recursive deletion."
    ;;
  BLOCK:docker_system_prune | BLOCK:docker_volume_remove | BLOCK:docker_compose_down_volumes)
    MESSAGE="Use the task-scoped Docker Compose operation or request user approval for volume cleanup."
    ;;
  BLOCK:drop_database | BLOCK:drop_table | BLOCK:truncate_table)
    MESSAGE="Change the schema with a new file in src/uzdigest/db/schema, tested on a copy of the database, with a backup first."
    ;;
  BLOCK:delete_database)
    MESSAGE="The live database and its backups are not deleted. Move what is not needed to _archive/ or ask the owner."
    ;;
  BLOCK:read_secrets)
    MESSAGE="Secrets stay in .env and are not read or printed. The names of the settings are in .env.example."
    ;;
  BLOCK:invalid_json | BLOCK:missing_tool_input | BLOCK:missing_command)
    MESSAGE="The hook could not validate the tool input. Retry with a valid command event or ask the user."
    ;;
  *)
    echo "BLOCKED [hook_validation_failed]: The security hook returned an unknown decision. Ask the user before retrying." >&2
    exit 2
    ;;
esac

REASON=${DECISION#BLOCK:}
echo "BLOCKED [$REASON]: $MESSAGE See .claude/rules/security.md." >&2
exit 2
