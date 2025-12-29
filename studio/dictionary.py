# studio/dictionary.py
import json
from pathlib import Path
import yaml

def load_dictionary(path: Path) -> dict:
    txt = path.read_text(encoding="utf-8")
    if path.suffix.lower() in [".yml", ".yaml"]:
        return yaml.safe_load(txt) or {}
    return json.loads(txt)

def to_json(d: dict) -> str:
    return json.dumps(d or {}, ensure_ascii=False)

def pretty(d: dict, max_chars: int = 8000) -> str:
    s = json.dumps(d or {}, ensure_ascii=False, indent=2)
    return s[:max_chars]

