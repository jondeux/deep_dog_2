#!/bin/bash
# Stage a Hugging Face Space repo from this checkout.
# Usage: webui/build_space.sh OUTPUT_DIR
# The output directory must be empty or not exist; nothing is written to the repo.
set -eu -o pipefail

out="${1:?usage: build_space.sh OUTPUT_DIR}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -e "$out" ] && [ -n "$(ls -A "$out")" ]; then
  echo "ERROR: $out exists and is not empty" >&2
  exit 1
fi
mkdir -p "$out"

cp "$root/webui/Dockerfile"           "$out/Dockerfile"
cp "$root/webui/space_README.md"      "$out/README.md"
cp "$root/webui/requirements-ui.txt"  "$out/requirements-ui.txt"
cp "$root/webui/app.py"               "$out/app.py"
cp "$root/pyproject.toml" "$root/LICENSE" "$out/"
cp -r "$root/deep_research"           "$out/deep_research"
find "$out" -name __pycache__ -type d -prune -exec rm -rf {} +

cat > "$out/.dockerignore" <<'EOF'
.git
__pycache__
*.pyc
.env
EOF

echo "Staged Space repo in $out"
ls -A "$out"
