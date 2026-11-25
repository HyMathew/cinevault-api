"""Validate exact pins and their installed transitive closure without network access."""

import argparse
import re
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

HASHED_LINE = re.compile(r"([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s+--hash=sha256:[0-9a-f]{64})+")


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


def docker_runtime_errors(requirements, lock):
    linux = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"}
    expected = {
        canonicalize_name(req.name): str(req.specifier)
        for req in read_requirements(requirements)
        if not req.marker or req.marker.evaluate(linux)
    }
    selected = {}
    errors = []
    content = lock.read_text(encoding="utf-8").replace("\\\n", "")
    for number, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = HASHED_LINE.fullmatch(line)
        if match is None:
            errors.append(f"{lock.name}:{number}: expected an exact pin and wheel SHA-256 hashes")
            continue
        name = canonicalize_name(match[1])
        if name in selected:
            errors.append(f"{lock.name}:{number}: duplicate pin for {name}")
        selected[name] = "==" + match[2]
    for name in sorted(expected.keys() | selected.keys()):
        if expected.get(name) != selected.get(name):
            errors.append(
                f"{name}: Docker pin {selected.get(name)} differs from Linux pin {expected.get(name)}"
            )
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", nargs="+", type=Path)
    parser.add_argument("--docker-runtime", type=Path)
    args = parser.parse_args()
    if args.docker_runtime:
        if len(args.requirements) != 1:
            parser.error("--docker-runtime requires one portable requirements file")
        errors = docker_runtime_errors(args.requirements[0], args.docker_runtime)
        success = "Docker wheel SHA-256 lock matches the Linux runtime pins."
    else:
        errors = [error for path in args.requirements for error in lock_errors(path)]
        success = "Pinned versions and active dependency closure validated for this platform."
    for error in errors:
        print(error)
    if errors:
        raise SystemExit(1)
    print(success)


if __name__ == "__main__":
    main()
