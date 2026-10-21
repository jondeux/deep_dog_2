"""Test helper (not a test): start the real webui/app.py with the model catalog's base
URLs pointed at a local fake provider.

    python tests/webui_launcher.py '<json {provider: base_url}>' --host 127.0.0.1 --port N

The override is an explicit assignment into model_catalog.BASE_URLS made here, in the
test process tree. The app itself never reads base URLs from the environment.
"""
import json
import runpy
import sys
from pathlib import Path

webui = Path(__file__).resolve().parents[1] / "webui"
sys.path.insert(0, str(webui))
import model_catalog  # noqa: E402

model_catalog.BASE_URLS.update(json.loads(sys.argv[1]))
sys.argv = [str(webui / "app.py"), *sys.argv[2:]]
runpy.run_path(str(webui / "app.py"), run_name="__main__")
