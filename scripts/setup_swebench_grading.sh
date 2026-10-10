#!/usr/bin/env bash
# Grading environment for SWE-bench Verified on Modal.
#
# The official harness (swebench 4.0.3) has a Modal mode, but its Modal code was
# written for Modal < 1.0 and Modal's servers reject old clients. This installs
# swebench 4.0.3 with Modal 1.6.1 in its own venv and applies
# scripts/swebench_modal.patch: Modal 1.x API changes, the official prebuilt
# x86_64 images (so results match local Docker grading), stderr kept in order,
# and sandboxes always terminated (upstream leaves them running).
#
#   bash scripts/setup_swebench_grading.sh        # venv at ~/.venvs/swebench
#   ~/.venvs/swebench/bin/modal token new         # once, to log in to Modal
set -euo pipefail
VENV="${SWEBENCH_VENV:-$HOME/.venvs/swebench}"
PY="${PYTHON:-python3.12}"
HERE="$(cd "$(dirname "$0")" && pwd)"

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q "swebench==4.0.3" "modal==1.6.1"
SITE="$("$VENV/bin/python" -c 'import os, swebench; print(os.path.dirname(os.path.dirname(swebench.__file__)))')"
if patch -d "$SITE" -p1 --forward --dry-run < "$HERE/swebench_modal.patch" >/dev/null 2>&1; then
  patch -d "$SITE" -p1 --forward < "$HERE/swebench_modal.patch"
else
  echo "patch already applied (or swebench version differs); leaving $SITE as is"
fi
echo "Grading venv ready: $VENV"
echo "Log in to Modal once with: $VENV/bin/modal token new"
