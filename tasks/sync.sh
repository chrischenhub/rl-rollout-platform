#!/bin/bash
# Copy the shared environment and grader into every task, and the task's instruction
# into its tests/ (Harbor uploads tests/ to the verifier, and the LLM judge reads it).
# Run after editing anything in _shared/ or an instruction.md.
set -euo pipefail
cd "$(dirname "$0")"
for task in */; do
  task=${task%/}
  [[ -f "$task/task.toml" ]] || continue
  cp _shared/Dockerfile "$task/environment/Dockerfile"
  cp _shared/grade.py _shared/conftest.py _shared/pytest.ini _shared/test.sh "$task/tests/"
  cp "$task/instruction.md" "$task/tests/instruction.md"
  chmod +x "$task/tests/test.sh" "$task/solution/solve.sh"
  echo "synced $task"
done
