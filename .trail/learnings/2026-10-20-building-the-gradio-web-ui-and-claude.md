---
title: Building the Gradio web UI and Space for deep_dog_2
author: Claude
date: 2026-10-20
branch: main
cost: { minutes: 43, tokens: 733739 }
touches:
  - tests/test_webui.py
  - webui/Dockerfile
  - webui/__init__.py
  - webui/app.py
  - webui/build_space.sh
  - webui/requirements-ui.txt
  - webui/space_README.md
  - tests/test_simple_research.py
---

## Decided
- The UI lives in webui/ (app.py, Dockerfile, space_README.md, requirements-ui.txt, build_space.sh). `webui/build_space.sh OUT_DIR` stages a Space repo, so nothing is written into the engine tree. Tests are in tests/test_webui.py and use the repo's FakeModelFactory.
- Provider keys come from the page as a per-run `Credentials` object, never from Space secrets. `Credentials.get` falls back to os.environ, so a provider key set as a Space secret would be spent by every visitor.
- Operator caps are Space variables: UI_MAX_MINUTES and UI_MAX_CONCURRENT_RUNS. ACCESS_PASSWORD is an optional shared-password gate.

## Tried and ruled out
- Gradio 6 `launch()` has no `show_api`: use `footer_links=["gradio"]` and `api_name=False` on handlers. The theme argument also moved from Blocks() to launch().
- A handler declared `f(..., *rest, request: gr.Request)` is never given the request (Gradio raises "missing keyword-only argument"). Use explicit parameters with `request: gr.Request = None`.
- A password Textbox cannot be multi-line, so extra keys are NAME=value pairs separated by semicolons.
- `pkill -f <name>` from a shell whose own command line contains the name kills the shell (exit 144). Kill by port or PID.

## Watch out
- Unit tests that call `run_ui` directly with `request=` passed in hid both the `request` bug and a `main()` crash. Run the real server and drive it with headless Chromium before trusting the UI.
- No Docker daemon in the workspace and huggingface.co is not on its allowlist, so the image build and the Space deploy have not been run. The stand-in check was a clean Python 3.11 venv installing the staged tree and serving it.
- The two failures in tests/test_simple_research.py (stale QUESTION reference) predate this work.
