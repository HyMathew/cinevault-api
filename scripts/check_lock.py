"""Validate exact pins and their installed transitive closure without network access."""

import argparse
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def read_requirements(path, seen=None):
    path = path.resolve()
    seen = set() if seen is None else seen
    if path in seen:
        return []
    seen.add(path)
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            result.extend(read_requirements(path.parent / line[3:].strip(), seen))
        else:
            result.append(Requirement(line))
    return result


def lock_errors(path, environment=None):
    environment = {**default_environment(), **(environment or {})}
    selected = {}
    errors = []
    for requirement in read_requirements(path):
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        pins = list(requirement.specifier)
        name = canonicalize_name(requirement.name)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version or requirement.url:
            errors.append(f"{name}: exact version pin required")
            continue
        if name in selected and str(selected[name].specifier) != str(requirement.specifier):
            errors.append(f"{name}: conflicting pins")
        selected[name] = requirement
    pending = [(name, frozenset(req.extras)) for name, req in selected.items()]
    visited = set()
    while pending:
        name, extras = pending.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            errors.append(f"{name}: locked package is not installed")
            continue
        if not selected[name].specifier.contains(distribution.version, prereleases=True):
            errors.append(f"{name}: installed {distribution.version} differs from lock")
        for value in distribution.requires or []:
            dependency = Requirement(value)
            contexts = ({**environment, "extra": extra} for extra in ("", *extras))
            if dependency.marker and not any(
                dependency.marker.evaluate(context) for context in contexts
            ):
                continue
            identity = canonicalize_name(dependency.name)
            if identity not in selected:
                errors.append(f"{name}: active dependency {identity} is not locked")
                continue
            pin = next(iter(selected[identity].specifier)).version
            if not dependency.specifier.contains(pin, prereleases=True):
                errors.append(f"{name}: {identity} pin does not satisfy {dependency.specifier}")
            pending.append((identity, frozenset(dependency.extras)))
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", nargs="+", type=Path)
    args = parser.parse_args()
    errors = [error for path in args.requirements for error in lock_errors(path)]
    for error in errors:
        print(error)
    if errors:
        raise SystemExit(1)
    print("Pinned versions and active dependency closure validated for this platform.")


if __name__ == "__main__":
    main()
