"""Summarize saved bundle entrypoints; no device/UI actions or RPC calls."""
import json
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summarize(path):
    text = path.read_text(encoding="utf-8")
    bundle = json.loads(text[text.index("{"):])
    modules = bundle.get("hapModuleInfos", [])
    result = {"source": str(path.resolve()), "saved_metadata_only": True,
              "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "not_a_device_probe": True, "modules": []}
    for module in modules:
        item = {"name": module.get("moduleName", module.get("name")),
                "hasIntent": module.get("hasIntent"),
                "abilities": [], "extensions": []}
        for field, destination in (("abilityInfos", "abilities"), ("extensionInfos", "extensions"),
                                   ("extensionAbilityInfos", "extensions")):
            for ability in module.get(field, []):
                selected = {key: ability[key] for key in ("name", "type", "visible", "exported", "permissions",
                                                         "readPermission", "writePermission", "uri", "skills", "metadata")
                            if key in ability}
                item[destination].append(selected)
        result["modules"].append(item)
    return result


if __name__ == "__main__":
    result = summarize(ROOT / "output/notes-research/hinote-bundle.json")
    destination = ROOT / "output/notes-research/entrypoints-review-20261016.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(destination), "modules": [
        {"name": m["name"], "hasIntent": m["hasIntent"],
         "abilities": [{k: a.get(k) for k in ("name", "visible")} for a in m["abilities"]],
         "extensions": [{k: a.get(k) for k in ("name", "type", "visible")} for a in m["extensions"]]}
        for m in result["modules"]]}, ensure_ascii=True, indent=2))
