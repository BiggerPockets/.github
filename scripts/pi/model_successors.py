#!/usr/bin/env python3
"""Find newer releases of the models pinned in scripts/pi/models.json, and optionally
add them to it.

A model family on OpenRouter keeps its shape from release to release and only the
version changes: openai/gpt-5.6-terra is followed by openai/gpt-6.1-terra,
anthropic/claude-haiku-4.5 by anthropic/claude-haiku-5.5. So a model's family is its
id with every version number blanked out, and a successor is a model in the same
family with a higher version. Tiers and snapshots stay apart because they change the
shape: gpt-6-luna-pro is not a Luna, and deepseek-v4-flash-0731 (a dated snapshot) is
not a deepseek-v4.1-flash. Batch variants (`:batch`) and moving aliases (`~...-latest`)
are never offered, since a pin should name one fixed model.

Only the newest release in each family is reported, and nothing is reported for a
family whose newest release is already pinned.

Adding a model to the catalog changes no review: a stage runs the model its workflow
input or Actions variable names, and the catalog only lists what is allowed. So the
weekly check can add successors on its own and leave switching to a person.

models.json repeats its "//" comment key, which json.load collapses to one, so the
file is edited as text: each new entry goes in directly before the entry it
succeeds, and the result is parsed again before it is written.

Prints {"successors": [...], "error": str | null}. Never raises and always exits 0;
a catalog that cannot be fetched is reported, not fatal.

Usage: model_successors.py [--write] <path-to-models.json>
"""
from datetime import datetime, timezone
import json
import re
import sys
import urllib.request

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
VERSION = re.compile(r"\d+(?:\.\d+)*")


def family(model_id):
    """The id with each version number replaced by '#', or None for an id that is
    never a pin: a batch variant or a moving alias."""
    if ":" in model_id or model_id.startswith("~"):
        return None
    return VERSION.sub("#", model_id)


def version(model_id):
    """Every version number in the id, in order, as integer tuples, so 6.1 > 5.6 and
    4.10 > 4.9."""
    return tuple(tuple(int(part) for part in match.split(".")) for match in VERSION.findall(model_id))


def per_million(price):
    try:
        return round(float(price) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def rates(entry):
    pricing = entry.get("pricing") or {}
    return {"input": per_million(pricing.get("prompt")),
            "output": per_million(pricing.get("completion"))}


def display_name(entry, model_id):
    """OpenRouter names read "OpenAI: GPT-6.1 Sol"; the catalog uses the part after
    the vendor."""
    name = entry.get("name") or model_id
    return name.split(": ", 1)[1] if ": " in name else name


def find_successors(pinned_ids, catalog):
    """For each pinned model, the newest unpinned release of its family, if newer.

    `catalog` maps model id to OpenRouter's catalog entry."""
    pinned = set(pinned_ids)
    newest = {}
    for model_id in catalog:
        key = family(model_id)
        if key is None:
            continue
        if key not in newest or version(model_id) > version(newest[key]):
            newest[key] = model_id

    successors = []
    reported = set()
    for pinned_id in pinned_ids:
        key = family(pinned_id)
        candidate = newest.get(key)
        if candidate is None or candidate in pinned or candidate in reported:
            continue
        if version(candidate) <= version(pinned_id):
            continue
        # A newer release of the family is already pinned: this pin is just older.
        if any(family(other) == key and version(other) >= version(candidate) for other in pinned):
            continue
        entry = catalog[candidate]
        params = entry.get("supported_parameters") or []
        created = entry.get("created")
        successors.append({
            "pinned": pinned_id,
            "successor": candidate,
            "name": display_name(entry, candidate),
            "reasoning": "reasoning" in params or "include_reasoning" in params,
            "released": (datetime.fromtimestamp(created, timezone.utc).date().isoformat()
                         if isinstance(created, (int, float)) else None),
            "pinned_rates": rates(catalog.get(pinned_id) or {}),
            "successor_rates": rates(entry),
        })
        reported.add(candidate)
    return successors


def insert_successors(text, successors):
    """models.json text with an entry for each successor placed directly before the
    entry it succeeds. Raises ValueError if an anchor entry can't be found or the
    result doesn't parse."""
    lines = text.split("\n")
    for successor in successors:
        anchor = f'"id": {json.dumps(successor["pinned"])},'
        id_line = next((i for i, line in enumerate(lines) if line.strip() == anchor), None)
        if id_line is None or id_line == 0 or lines[id_line - 1].strip() != "{":
            raise ValueError(f"could not find the entry for {successor['pinned']}")
        open_line = id_line - 1
        outer = lines[open_line][: len(lines[open_line]) - len(lines[open_line].lstrip())]
        inner = lines[id_line][: len(lines[id_line]) - len(lines[id_line].lstrip())]
        block = [
            f"{outer}{{",
            f'{inner}"id": {json.dumps(successor["successor"])},',
            f'{inner}"name": {json.dumps(successor["name"])},',
            f'{inner}"reasoning": {"true" if successor["reasoning"] else "false"}',
            f"{outer}}},",
        ]
        lines[open_line:open_line] = block
    result = "\n".join(lines)
    json.loads(result)
    return result


def load_pinned_ids(text):
    return [model["id"] for model in json.loads(text)["providers"]["openrouter"]["models"]]


def fetch_catalog():
    """({model_id: entry}, None), or ({}, error message)."""
    try:
        with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=30) as response:
            payload = json.load(response)
    except Exception as error:
        return {}, str(error)
    return {entry["id"]: entry for entry in payload.get("data", []) if entry.get("id")}, None


def main(argv):
    args = [arg for arg in argv[1:] if arg != "--write"]
    write = "--write" in argv[1:]
    path = args[0] if args else "scripts/pi/models.json"
    result = {"successors": [], "error": None}

    try:
        with open(path) as handle:
            text = handle.read()
        pinned_ids = load_pinned_ids(text)
    except Exception as error:
        result["error"] = f"could not read {path}: {error}"
        print(json.dumps(result))
        return 0

    catalog, error = fetch_catalog()
    if error:
        result["error"] = f"could not fetch OpenRouter catalog: {error}"
        print(json.dumps(result))
        return 0

    result["successors"] = find_successors(pinned_ids, catalog)
    if write and result["successors"]:
        try:
            updated = insert_successors(text, result["successors"])
        except ValueError as error:
            result["error"] = f"could not update {path}: {error}"
            print(json.dumps(result))
            return 0
        with open(path, "w") as handle:
            handle.write(updated)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
