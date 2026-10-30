"""Prepare the host's Kerberos profile for the data-transfer container.

gokrb5 does not expand MIT include directives, and repeated [realms] sections
replace earlier ones. Expand the files and combine sections before mounting the
result for the container's SMB tools. Unambiguous final tags are preserved by
flattening; ambiguous repeats require an explicit profile. Native host tools
continue to use the original profile.
"""
import argparse
import os
from pathlib import Path
import re
import tempfile


class ProfileError(ValueError):
    pass


def uncomment(line):
    quoted = escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char in "#;" and not quoted:
            return line[:index].strip()
    return line.strip()


def load_profile(path, sections, active=(), seen=None, finals=None):
    # A unique final section/subsection/relation can be flattened unchanged.
    # Repeated final tags have version-dependent MIT semantics (changed in
    # 1.22); refuse those instead of silently changing the effective policy.
    seen = {} if seen is None else seen
    finals = {} if finals is None else finals
    def occurrence(tag, location):
        seen[tag] = seen.get(tag, 0) + 1
        for prefix, marked in finals.items():
            if tag[:len(prefix)] == prefix:
                raise ProfileError(f"repeated final Kerberos tag needs an explicit flattened SMB_KRB5_CONF ({marked}; {location})")
    def final(tag, location):
        if seen.get(tag, 0) != 1:
            raise ProfileError(f"repeated final Kerberos tag needs an explicit flattened SMB_KRB5_CONF ({location})")
        finals[tag] = location
    path = Path(path).resolve(strict=True)
    if path in active or len(active) >= 32:
        raise ProfileError(f"Kerberos include cycle or excessive nesting at {path}")
    if path.is_dir():
        # KRB5_CONFIG directory names accept the original MIT filename subset.
        for child in sorted(path.iterdir()):
            if child.is_file() and re.fullmatch(r"[A-Za-z0-9_-]+", child.name):
                load_profile(child, sections, (*active, path), seen, finals)
        return
    current, stack, tag = None, [], ()
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = uncomment(raw)
        if not line:
            continue
        location = f"{path}:{number}"
        directive = re.fullmatch(r"(include|includedir)\s+(.+)", line)
        if directive:
            if stack:
                raise ProfileError(f"include inside a subsection at {location}")
            kind, name = directive.groups()
            included = Path(name.strip().strip('"'))
            if not included.is_absolute():
                raise ProfileError(f"include must use an absolute path at {location}")
            if kind == "include":
                load_profile(included, sections, (*active, path), seen, finals)
            else:
                for child in sorted(included.iterdir()):
                    if child.is_file() and (re.fullmatch(r"[A-Za-z0-9_-]+", child.name)
                                            or (not child.name.startswith(".") and child.name.endswith(".conf"))):
                        load_profile(child, sections, (*active, path), seen, finals)
            continue
        if re.match(r"module\s+(?![=\s])", line):
            raise ProfileError(f"dynamic profiles need an explicit flattened SMB_KRB5_CONF ({location})")
        section = re.fullmatch(r"\[([^\]]+)\](\*)?", line)
        if section:
            if stack:
                raise ProfileError(f"unclosed subsection at {location}")
            tag = (section.group(1),)
            occurrence(tag, location)
            current = sections.setdefault(tag[0], {})
            if section.group(2):
                final(tag, location)
            continue
        if line in ("}", "}*"):
            if not stack:
                raise ProfileError(f"unexpected closing brace at {location}")
            if line == "}*":
                final(tag, location)
            current, tag = stack.pop()
            continue
        if current is None or "=" not in line:
            raise ProfileError(f"invalid Kerberos profile at {location}")
        key, value = (part.strip() for part in line.split("=", 1))
        marked = key.endswith("*")
        key = key[:-1].rstrip() if marked else key
        if not key:
            raise ProfileError(f"unsupported relation at {location}")
        child_tag = (*tag, key)
        # A final ancestor only forbids a later occurrence of that ancestor,
        # not the remaining contents of its first definition.
        if child_tag in finals:
            raise ProfileError(f"repeated final Kerberos tag needs an explicit flattened SMB_KRB5_CONF ({location})")
        seen[child_tag] = seen.get(child_tag, 0) + 1
        if value == "{":
            child = {}
            existing = current.setdefault(key, [])
            if existing and isinstance(existing[0], dict):
                child = existing[0]
            else:
                existing.append(child)
            stack.append((current, tag))
            current = child
            tag = child_tag
            if marked:
                final(tag, location)
        else:
            if value.startswith("{"):
                raise ProfileError(f"use multiline subsections in SMB_KRB5_CONF ({location})")
            current.setdefault(key, []).append(value)
            if marked:
                final(child_tag, location)
    if stack:
        raise ProfileError(f"unclosed subsection in {path}")


def render_relations(relations, indent="", scalar_first=False):
    lines = []
    for name, values in relations.items():
        for value in (values[:1] if scalar_first else values):
            if isinstance(value, dict):
                lines.append(f"{indent}{name} = {{")
                lines.extend(render_relations(value, indent + "    "))
                lines.append(indent + "}")
            else:
                lines.append(f"{indent}{name} = {value}")
    return lines


def prepare(sources, destination, cache, realm=""):
    sections = {}
    seen, finals = {}, {}
    for source in sources:
        load_profile(source, sections, seen=seen, finals=finals)
    defaults = sections.setdefault("libdefaults", {})
    defaults.setdefault("dns_lookup_kdc", ["true"])
    defaults["default_ccache_name"] = ["FILE:" + cache]
    if realm:
        defaults["default_realm"] = [realm]
    canonical = defaults.get("dns_canonicalize_hostname", [])
    if canonical and canonical[0] == "fallback":
        defaults["dns_canonicalize_hostname"] = ["false"]
    lines = ["# Generated for hpclib data transfer; the host profile is unchanged."]
    for name, values in sections.items():
        lines.append(f"\n[{name}]")
        # MIT uses the first scalar value. Avoid gokrb5's last-value behavior.
        lines.extend(render_relations(values, scalar_first=name in ("libdefaults", "domain_realm")))
    text = "\n".join(lines) + "\n"
    destination = Path(destination)
    if destination.is_symlink():
        raise ProfileError(f"refusing a symlink for generated profile: {destination}")
    fd, temporary = tempfile.mkstemp(prefix=".krb5-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--realm", default="")
    args = parser.parse_args()
    try:
        prepare(args.source, args.output, args.cache, args.realm)
    except (OSError, ProfileError) as error:
        parser.exit(1, f"smbshell: cannot prepare Kerberos configuration: {error}\n")


if __name__ == "__main__":
    main()
