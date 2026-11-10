#!/usr/bin/env python3
"""Build the /deep-dog-Research plugin (a .plugin zip) from the repo's skill file.

    python3 scripts/build_plugin.py [VERSION] [--out DIR]

Upload the result under Customize > Plugins in claude.ai. To update an uploaded copy, build again with a higher
VERSION and upload it: the repo's `skills-src/deep-dog/SKILL.md` stays the one source (kept out of `.claude/skills/` on purpose, so the project does not load a second copy next to the plugin). The skill drives
`run_research.py` and `docs/`, so it works in a session that has this repo checked out."""
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills-src" / "deep-dog" / "SKILL.md"
NAME = "deep-dog-research"


def skill_for_plugin(text: str) -> tuple:
    """``(skill name, SKILL.md text)`` made valid for a plugin: the Agent Skills rules are lowercase letters, digits and hyphens
    in the name (64 at most) and a description of 1024 characters or fewer with no angle brackets (the uploader rejects XML
    tags there). The repo's own skill keeps the name `deep-dog-Research`; the plugin copy is lowercased."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError("SKILL.md has no frontmatter")
    fields = dict(re.findall(r"^([a-z-]+): (.*)$", m.group(1), re.M))
    name, description = fields.get("name", "").strip().lower(), fields.get("description", "").strip()
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError(f"skill name {name!r} must be lowercase letters, digits and hyphens, at most 64 characters")
    if not description or len(description) > 1024:
        raise ValueError("skill description must be 1 to 1024 characters")
    if "<" in description or ">" in description:
        raise ValueError("skill description cannot contain angle brackets (XML tags)")
    extra = set(fields) - {"name", "description"}
    if extra:
        raise ValueError(f"unexpected frontmatter fields: {sorted(extra)}")
    body = text[m.end():]
    return name, f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def build(version: str = "1.0.0", out: Path = ROOT / "skill-export") -> Path:
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": NAME, "displayName": "Deep Dog Research", "version": version,
        "description": "/deep-dog-Research: runs the Deep Dog 2 research engine (a cited report) on your own Claude plan, "
                       "with a model and effort picker per role, optional third-party models and every engine setting as a "
                       "switch. Skill only: nothing runs until you invoke it, and it never approves its own spend.",
        "author": {"name": "jondeux", "url": "https://github.com/jondeux"},
        "repository": "https://github.com/jondeux/deep_dog_2",
        "keywords": ["research", "deep-research", "claude-plan", "report"],
    }
    skill_name, skill_text = skill_for_plugin(SKILL.read_text(encoding="utf-8"))
    readme = (f"# Deep Dog Research {version}\n\nOne skill, `{skill_name}`. It needs a session with the deep_dog_2 repo "
              "checked out (it runs `run_research.py`). See `docs/DEEP_DOG_RESEARCH_MANUAL.md` in the repo.\n")
    target = out / f"{NAME}-{version}.plugin"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(".claude-plugin/plugin.json", json.dumps(manifest, indent=2) + "\n")
        z.writestr("README.md", readme)
        z.writestr(f"skills/{skill_name}/SKILL.md", skill_text)
    return target


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "skill-export"
    if "--out" in sys.argv:
        args = [a for a in args if a != str(out)]
    print(build(args[0] if args else "1.0.0", out))
