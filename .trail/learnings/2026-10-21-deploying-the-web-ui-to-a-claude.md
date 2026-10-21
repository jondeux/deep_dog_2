---
title: Deploying the web UI to a free-tier Hugging Face Space (Gradio SDK on ZeroGPU)
author: Claude
date: 2026-10-21
branch: main
cost: { minutes: 27, tokens: 200753 }
touches:
  - webui/app.py
  - webui/build_space.sh
  - tests/test_webui.py
  - webui/Dockerfile
  - webui/__init__.py
  - webui/requirements-ui.txt
  - webui/space_README.md
  - requirements.txt
  - pyproject.toml
---

## Decided
- Free HF accounts cannot create Docker or CPU Gradio Spaces; they can host up to 2 ZeroGPU Spaces, which must use the Gradio SDK. The live Space runs that way.
- Stage it with `webui/build_space.sh OUT gradio` (default). README header: sdk: gradio, sdk_version: 6.30.0, python_version: "3.11", app_file: app.py.
- requirements.txt is generated from pyproject.toml (dependencies + the google extra) plus gradio==6.30.0. app.py imports deep_research from its own folder; the package is not pip-installed.
- app.py defines a never-called `@spaces.GPU` marker inside try/except ImportError, so ZeroGPU starts the Space; elsewhere it is skipped.
- app.py reads GRADIO_SERVER_NAME / GRADIO_SERVER_PORT so it binds 0.0.0.0 on Spaces.

## Tried and ruled out
- A "." line in requirements.txt: the Gradio-SDK build runs `pip install -r requirements.txt` before the repo files are copied, so it fails with "Directory '.' is not installable".
- Starting on ZeroGPU without any @spaces.GPU function: runtime error "No @spaces.GPU function detected during startup".

## Watch out
- The first failing build ran python:3.10.13 although the engine requires >=3.11; check that the uploaded README keeps python_version: "3.11".
- ZeroGPU adds torch and spaces==0.51.3 to the image; the build is slower but works.
- Free Spaces sleep when idle; long research runs may be cut off.
