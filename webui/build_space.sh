#!/bin/bash
# Stage a Hugging Face Space repo from this checkout.
# Usage: webui/build_space.sh OUTPUT_DIR [gradio|docker]    (default: gradio)
# The output directory must be empty or not exist; nothing is written to the repo.
set -eu -o pipefail

out="${1:?usage: build_space.sh OUTPUT_DIR [gradio|docker]}"
sdk="${2:-gradio}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$sdk" in gradio|docker) ;; *) echo "ERROR: sdk must be gradio or docker" >&2; exit 1 ;; esac

if [ -e "$out" ] && [ -n "$(ls -A "$out")" ]; then
  echo "ERROR: $out exists and is not empty" >&2
  exit 1
fi
mkdir -p "$out"

cp "$root/webui/app.py"               "$out/app.py"
cp "$root/pyproject.toml" "$root/LICENSE" "$out/"
cp -r "$root/deep_research"           "$out/deep_research"
find "$out" -name __pycache__ -type d -prune -exec rm -rf {} +

if [ "$sdk" = docker ]; then
  cp "$root/webui/Dockerfile"          "$out/Dockerfile"
  cp "$root/webui/space_README.md"     "$out/README.md"
  cp "$root/webui/requirements-ui.txt" "$out/requirements-ui.txt"
  printf '.git\n__pycache__\n*.pyc\n.env\n' > "$out/.dockerignore"
else
  # Gradio SDK: no Dockerfile. Spaces runs `pip install -r requirements.txt` in a
  # build step that happens BEFORE the repo files are copied in, so a "." line
  # cannot work. List the engine's dependencies explicitly instead (generated from
  # pyproject.toml so they cannot drift); app.py imports deep_research from its own
  # directory, so the package itself needs no install.
  sed -e 's/^sdk: docker$/sdk: gradio\nsdk_version: 6.30.0\npython_version: "3.11"\napp_file: app.py/' \
      -e '/^app_port:/d' "$root/webui/space_README.md" > "$out/README.md"
  python3 - "$root/pyproject.toml" "$out/requirements.txt" <<'PY'
import sys, tomllib
proj = tomllib.load(open(sys.argv[1], "rb"))["project"]
deps = ["gradio==6.30.0"] + proj["dependencies"] + proj["optional-dependencies"]["google"]
open(sys.argv[2], "w").write("\n".join(deps) + "\n")
PY
fi

echo "Staged $sdk Space repo in $out"
ls -A "$out"
