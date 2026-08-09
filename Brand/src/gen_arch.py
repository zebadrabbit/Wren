#!/usr/bin/env python3
"""Measure Wren's own plugin graph and print it as JSON.

This is the Brand pack's equivalent of a measurement pass: the architecture
figure in the README is not a diagram somebody drew and then forgot to update,
it is a rendering of what `wren/registry.py` and `wren/communication/` actually
contain right now. Add a skill and re-run the build; the figure grows a node.

Parsed with `ast`, deliberately NOT imported: importing `wren.config` requires a
populated .env and a reachable LLM provider, which a brand build has no business
needing.

    python3 gen_arch.py [path/to/repo] > arch.json
"""
import ast
import json
import os
import sys


def _literal(mod_ast, name):
    """Value of a module-level assignment, or None."""
    for node in mod_ast.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:
                        return None
    return None


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _has_not_implemented(mod_ast):
    for node in ast.walk(mod_ast):
        if isinstance(node, ast.Raise):
            exc = node.exc
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                if exc.func.id == "NotImplementedError":
                    return True
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
    return False


def _registry_skills(registry_path):
    """The module names in registry.py's PLUGINS list, in declared order."""
    tree = _parse(registry_path)
    names = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "PLUGINS":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for el in node.value.elts:
                            if isinstance(el, ast.Name):
                                names.append(el.id)
    return names


def measure(repo):
    pkg = os.path.join(repo, "wren")
    comm_dir = os.path.join(pkg, "communication")
    skills_dir = os.path.join(pkg, "skills")

    communication = []
    if os.path.isdir(comm_dir):
        for fn in sorted(os.listdir(comm_dir)):
            if not fn.endswith("_plugin.py"):
                continue
            tree = _parse(os.path.join(comm_dir, fn))
            mod = fn[:-3]
            communication.append({
                "module": mod,
                "key": mod[:-len("_plugin")],
                "name": _literal(tree, "PLUGIN_NAME") or mod,
                "role": _literal(tree, "ROLE") or "chat",
                "canNotify": bool(_literal(tree, "CAN_NOTIFY"))
                if _literal(tree, "CAN_NOTIFY") is not None else None,
                "stub": _has_not_implemented(tree),
            })

    declared = _registry_skills(os.path.join(pkg, "registry.py"))
    skills = []
    if os.path.isdir(skills_dir):
        for mod in declared:
            path = os.path.join(skills_dir, mod + ".py")
            if not os.path.exists(path):
                continue
            tree = _parse(path)
            intents = _literal(tree, "INTENTS") or []
            skills.append({
                "module": mod,
                "name": _literal(tree, "PLUGIN_NAME") or mod.replace("_skill", "").title(),
                "intents": list(intents),
            })

    return {
        "communication": communication,
        "skills": skills,
        "totals": {
            "chat": sum(1 for c in communication if c["role"] == "chat" and not c["stub"]),
            "input": sum(1 for c in communication if c["role"] == "input"),
            "stubs": sum(1 for c in communication if c["stub"]),
            "skills": len(skills),
            "intents": sum(len(s["intents"]) for s in skills),
        },
    }


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..")
    data = measure(os.path.abspath(repo))
    if not data["skills"] and not data["communication"]:
        sys.exit("measured nothing — is %s a Wren checkout?" % os.path.abspath(repo))
    print(json.dumps(data, indent=1))
