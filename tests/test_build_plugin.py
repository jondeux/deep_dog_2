"""The .plugin build passes the uploader's rules: valid manifest, lowercase skill name, no XML tags in the description."""
import importlib.util
import json
import re
import zipfile

import pytest


def load():
    spec = importlib.util.spec_from_file_location("build_plugin", "scripts/build_plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_built_plugin_is_valid(tmp_path):
    bp = load()
    path = bp.build("9.9.9", tmp_path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        manifest = json.loads(z.read(".claude-plugin/plugin.json"))
        skill = z.read("skills/deep-dog-research/SKILL.md").decode()
    assert path.name == "deep-dog-research-9.9.9.plugin"
    assert names == [".claude-plugin/plugin.json", "README.md", "skills/deep-dog-research/SKILL.md"]
    assert manifest["name"] == "deep-dog-research" and manifest["version"] == "9.9.9"
    head = re.match(r"---\n(.*?)\n---\n", skill, re.S).group(1)
    desc = re.search(r"^description: (.*)$", head, re.M).group(1)
    assert "name: deep-dog-research" in head and "<" not in desc and ">" not in desc and len(desc) <= 1024


def test_the_repo_skill_description_has_no_angle_brackets():
    text = open(".claude/skills/deep-dog/SKILL.md", encoding="utf-8").read()
    desc = re.search(r"^description: (.*)$", text, re.M).group(1)
    assert "<" not in desc and ">" not in desc


@pytest.mark.parametrize("front, message", [
    ("name: Bad_Name\ndescription: ok", "lowercase"),
    ("name: ok\ndescription: use <question> here", "angle brackets"),
    ("name: ok\ndescription: " + "x" * 1025, "1024"),
    ("name: ok\ndescription: fine\nmodel: opus", "unexpected"),
])
def test_invalid_skills_are_refused(front, message):
    with pytest.raises(ValueError, match=message):
        load().skill_for_plugin(f"---\n{front}\n---\nbody\n")
