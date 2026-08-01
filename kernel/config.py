"""Product and objective pack loading.

A product is configuration, not code (DESIGN.md §5). This module is the only
place that reads a product directory, and it validates aggressively at load
time: a route naming a variable that no declaration produces should fail on
`study init`, not three weeks later in the middle of a session when the
allocator silently ranks everything at zero.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from kernel.objectives import routes as routes_mod


class PackError(ValueError):
    """Raised for a malformed or internally inconsistent product pack."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise PackError(f"missing required pack file: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise PackError(f"{path} must contain a YAML mapping")
    return data


def digest(product_dir: Path) -> str:
    """Hash of every pack file, so a config edit is detectable across sessions."""
    h = hashlib.sha256()
    for path in sorted(product_dir.rglob("*.yaml")):
        h.update(path.relative_to(product_dir).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def load_product(product_dir: Path) -> dict[str, Any]:
    """Load and validate a product pack into one merged config dict."""
    product_dir = Path(product_dir)
    product = _read_yaml(product_dir / "product.yaml")
    objective = _read_yaml(product_dir / "objective.yaml").get("objective")
    if not objective:
        raise PackError(f"{product_dir / 'objective.yaml'} has no top-level `objective:`")

    product["objective"] = objective
    product.setdefault("product_id", product_dir.name)
    product["_dir"] = str(product_dir)

    tags, edges = load_taxonomy(product_dir / "taxonomy")
    product["_tags"] = tags
    product["_edges"] = edges

    validate(product)
    return product


def load_taxonomy(taxonomy_dir: Path) -> tuple[list[dict], list[dict]]:
    """Read every taxonomy YAML in the directory into tags and seed edges."""
    tags: list[dict] = []
    edges: list[dict] = []
    if not taxonomy_dir.exists():
        return tags, edges

    for path in sorted(taxonomy_dir.glob("*.yaml")):
        data = _read_yaml(path)
        for tag in data.get("tags", []):
            if "slug" not in tag:
                raise PackError(f"{path}: every tag needs a slug")
            tags.append(tag)
        for edge in data.get("edges", []):
            if "parent" not in edge or "child" not in edge:
                raise PackError(f"{path}: every edge needs parent and child")
            edge.setdefault("source", "seed")
            edge.setdefault("confidence", 1.0)
            edges.append(edge)

    slugs = {t["slug"] for t in tags}
    for edge in edges:
        missing = {edge["parent"], edge["child"]} - slugs
        if missing:
            raise PackError(f"edge references undefined tag(s): {sorted(missing)}")
    return tags, edges


def _declared_variables(objective: dict[str, Any]) -> set[str]:
    """Every variable name the objective can produce, including nested inners."""
    names = set(objective.get("variables", {}) or {})
    names |= set((objective.get("domains", {}) or {}).get("variables", {}) or {})
    inner = objective.get("inner")
    if inner:
        names |= _declared_variables(inner)
    return names


def _route_expressions(objective: dict[str, Any]) -> list[str]:
    exprs: list[str] = []
    for group in (objective.get("routes") or {}).values():
        exprs.extend(group)
    inner = objective.get("inner")
    if inner:
        exprs.extend(_route_expressions(inner))
    return exprs


def validate(product: dict[str, Any]) -> None:
    """Fail loudly at load time on the inconsistencies that go quiet at runtime."""
    objective = product["objective"]
    sections = product.get("sections") or {}
    if not sections:
        raise PackError("product.yaml must declare at least one section")

    valid_policies = {"withheld", "pinned", "pinned_strict", "anchored"}
    for slug, spec in sections.items():
        policy = spec.get("explanation_policy", "withheld")
        if policy not in valid_policies:
            raise PackError(
                f"section {slug!r}: explanation_policy {policy!r} is not one of "
                f"{sorted(valid_policies)}"
            )

    declared = _declared_variables(objective)
    for expr in _route_expressions(objective):
        used = routes_mod.variables_used(expr)
        missing = used - declared
        if missing:
            raise PackError(
                f"route {expr!r} reads undeclared variable(s) {sorted(missing)}; "
                f"declared: {sorted(declared) or 'none'}"
            )

    section_slugs = set(sections)
    for name, spec in (objective.get("variables") or {}).items():
        section = spec.get("section")
        if section and section not in section_slugs:
            raise PackError(f"variable {name!r} references unknown section {section!r}")

    for tag in product.get("_tags", []):
        section = tag.get("section")
        if section and section not in section_slugs:
            raise PackError(
                f"tag {tag['slug']!r} references unknown section {section!r}"
            )
