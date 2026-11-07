"""Offline checks of local documentation, declared API routes and JSON examples."""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def markdown_errors(root):
    root = root.resolve()
    errors = []
    for path in [root / "README.md", *sorted((root / "docs").rglob("*.md"))]:
        if not path.is_file():
            errors.append(f"Missing document: {path.relative_to(root)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"Invalid UTF-8: {path.relative_to(root)}")
            continue
        for target in re.findall(r"!?\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)", text):
            target = target.strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = (path.parent / unquote(parsed.path)).resolve()
            if not destination.is_relative_to(root) or not destination.exists():
                errors.append(f"Broken/outside local link in {path.relative_to(root)}: {target}")
    return errors


def route_errors(app, spec):
    """Compare the contract with Flask's registered v1 rules, without requests."""
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    supported, required = set(), set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/v1/"):
            continue
        url = re.sub(r"<[^>]+>", "{}", rule.rule)
        registered = {method.lower() for method in rule.methods}
        supported.update((method, url) for method in registered)
        explicit = registered.copy()
        if "get" in registered:
            explicit.discard("head")
        if getattr(rule, "provide_automatic_options", False):
            explicit.discard("options")
        required.update((method, url) for method in explicit)
    declared = {
        (method, re.sub(r"\{[^}]+\}", "{}", url))
        for url, operations in spec["paths"].items()
        for method in operations
        if method in methods
    }
    errors = [
        f"Undocumented API operation: {method.upper()} {url}"
        for method, url in sorted(required - declared)
    ]
    errors.extend(
        f"Contract operation without route: {method.upper()} {url}"
        for method, url in sorted(declared - supported)
    )
    return errors


def contract_errors(root):
    contract = root / "docs/openapi-v1.json"
    if not contract.exists():
        return []
    from jsonschema import Draft202012Validator

    spec = json.loads(contract.read_text(encoding="utf-8"))
    errors = []
    examples = [
        (
            "catalogue-v2.example.json",
            json.loads((root / "schemas/catalogue-v2.schema.json").read_text(encoding="utf-8")),
        ),
        (
            "catalogue-transfer-v1.example.json",
            json.loads(
                (root / "schemas/catalogue-transfer-v1.schema.json").read_text(encoding="utf-8")
            ),
        ),
        (
            "collection.example.json",
            {"$ref": "#/components/schemas/Stored_collection", "components": spec["components"]},
        ),
        (
            "external-source.example.json",
            {"$ref": "#/components/schemas/ExternalSource", "components": spec["components"]},
        ),
    ]
    for filename, schema in examples:
        Draft202012Validator.check_schema(schema)
        value = json.loads((root / "schemas" / filename).read_text(encoding="utf-8"))
        errors.extend(
            f"Invalid {filename}: {item.message}"
            for item in Draft202012Validator(schema).iter_errors(value)
        )
    return errors


def main():
    root = Path(__file__).resolve().parents[1]
    errors = markdown_errors(root) + contract_errors(root)
    for error in errors:
        print(error)
    if errors:
        raise SystemExit(1)
    print("Documentation links and available contract/examples validated offline.")


if __name__ == "__main__":
    main()
