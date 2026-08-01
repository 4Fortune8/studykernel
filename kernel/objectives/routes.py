"""Route expressions: boolean conditions evaluated as probabilities.

DESIGN.md §7.1 makes multiple routes first-class -- a threshold objective is a
boolean expression over sub-conditions, e.g.

    crc_estimate >= 950
    all_domains(diagnostic_level >= 6)
    crc_estimate >= 945 AND essay_score >= 5

and the plugin evaluates P(success) per route so the allocator can push effort
toward the steepest one. A learner with deep algebra and hollow geometry is
closer via the no-holes route than via raw ability, and the allocator should
know it.

The expressions come from product config, so they are parsed with `ast` under
a node whitelist -- never `eval`. A malformed pack should raise, not execute.

**Independence assumption, stated plainly.** AND multiplies and OR uses a
noisy-or. Sub-conditions over the same learner are positively correlated in
reality, so AND under-estimates and OR over-estimates slightly. This is the
analytic approximation DESIGN.md §17 Q2 contemplates; the Monte Carlo sitting
simulation it prefers is a v1 item and slots in behind this same interface.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

from kernel.state.view import Estimate, StateView

# Products write AND/OR/NOT; Python's parser wants and/or/not.
_KEYWORD_FIXUPS = ((" AND ", " and "), (" OR ", " or "), ("NOT ", "not "))

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.GtE,
    ast.Gt,
    ast.LtE,
    ast.Lt,
    ast.Eq,
)

# Aggregate predicates. Each takes the *unevaluated* comparison and a state,
# and returns a probability.
AggregateFn = Callable[[ast.Compare, StateView], float]


class RouteError(ValueError):
    """Raised for a malformed or unsupported route expression."""


def normalize(expression: str) -> str:
    text = f" {expression.strip()} "
    for src, dst in _KEYWORD_FIXUPS:
        text = text.replace(src, dst)
    return text.strip()


def parse(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(normalize(expression), mode="eval")
    except SyntaxError as exc:
        raise RouteError(f"cannot parse route {expression!r}: {exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RouteError(
                f"route {expression!r} uses unsupported syntax {type(node).__name__}"
            )
    return tree


# --------------------------------------------------------------- evaluation


def _comparison_probability(var: Estimate, op: ast.cmpop, bound: float) -> float:
    """P(the comparison holds), given the variable's uncertainty."""
    if isinstance(op, ast.GtE | ast.Gt):
        return var.p_at_least(bound)
    if isinstance(op, ast.LtE | ast.Lt):
        return 1.0 - var.p_at_least(bound)
    if isinstance(op, ast.Eq):
        # Equality on a continuous estimate is only meaningful for the
        # discrete, learner-reported variables, where sd is 0.
        return 1.0 if abs(var.value - bound) < 1e-9 else 0.0
    raise RouteError(f"unsupported comparison operator {type(op).__name__}")


def _resolve(name: str, variables: dict[str, Estimate]) -> Estimate:
    if name not in variables:
        known = ", ".join(sorted(variables)) or "none"
        raise RouteError(f"route refers to unknown variable {name!r}; declared: {known}")
    return variables[name]


def _all_domains(comparison: ast.Compare, state: StateView) -> float:
    """Every domain independently satisfies the comparison.

    `domain_variables` is keyed domain -> variable -> Estimate, so this is the
    same comparison evaluated once per domain and multiplied. With no domains
    declared the predicate is vacuously true, which is the correct reading of
    "all" over an empty set but would silently pass a misconfigured pack --
    hence the explicit raise instead.
    """
    if not state.domain_variables:
        raise RouteError("all_domains(...) used but the product declares no domains")
    p = 1.0
    for domain, variables in state.domain_variables.items():
        p *= _evaluate_compare(comparison, variables, state, domain=domain)
    return p


def _any_domain(comparison: ast.Compare, state: StateView) -> float:
    if not state.domain_variables:
        raise RouteError("any_domain(...) used but the product declares no domains")
    q = 1.0
    for variables in state.domain_variables.values():
        q *= 1.0 - _evaluate_compare(comparison, variables, state)
    return 1.0 - q


AGGREGATES: dict[str, AggregateFn] = {
    "all_domains": _all_domains,
    "any_domain": _any_domain,
}


def _evaluate_compare(
    node: ast.Compare,
    variables: dict[str, Estimate],
    state: StateView,
    domain: str | None = None,
) -> float:
    if len(node.ops) != 1 or len(node.comparators) != 1:
        raise RouteError("chained comparisons are not supported in routes")
    if not isinstance(node.left, ast.Name):
        raise RouteError("the left side of a route comparison must be a variable name")
    comparator = node.comparators[0]
    if not isinstance(comparator, ast.Constant) or not isinstance(
        comparator.value, int | float
    ):
        raise RouteError("the right side of a route comparison must be a number")

    var = _resolve(node.left.id, variables)
    return _comparison_probability(var, node.ops[0], float(comparator.value))


def _evaluate(node: ast.AST, state: StateView) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, state)

    if isinstance(node, ast.BoolOp):
        parts = [_evaluate(v, state) for v in node.values]
        if isinstance(node.op, ast.And):
            p = 1.0
            for part in parts:
                p *= part
            return p
        # noisy-or
        q = 1.0
        for part in parts:
            q *= 1.0 - part
        return 1.0 - q

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return 1.0 - _evaluate(node.operand, state)

    if isinstance(node, ast.Compare):
        return _evaluate_compare(node, state.variables, state)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in AGGREGATES:
            allowed = ", ".join(sorted(AGGREGATES))
            raise RouteError(f"routes may only call: {allowed}")
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Compare):
            raise RouteError(f"{node.func.id}(...) takes exactly one comparison")
        return AGGREGATES[node.func.id](node.args[0], state)

    raise RouteError(f"unsupported node {type(node).__name__} in route expression")


def probability(expression: str, state: StateView) -> float:
    """P(this route succeeds) given current state."""
    return _evaluate(parse(expression), state)


def variables_used(expression: str) -> set[str]:
    """Variable names a route reads. Used to validate packs at load time."""
    return {n.id for n in ast.walk(parse(expression)) if isinstance(n, ast.Name)} - set(
        AGGREGATES
    )


def satisfied_with_margin(
    expression: str, state: StateView, margin: dict[str, float]
) -> bool:
    """Whether the route holds after every variable is docked its margin.

    Aiming at exactly the line is a coin flip on test day (DESIGN.md §7.1), so
    the stopping rule tests a pessimistic copy of the state rather than the
    point estimate. `margin` is per-variable, in that variable's own units.
    """
    docked = {
        name: Estimate(est.value - margin.get(name, 0.0), 0.0)
        for name, est in state.variables.items()
    }
    docked_domains = {
        domain: {
            name: Estimate(est.value - margin.get(name, 0.0), 0.0)
            for name, est in variables.items()
        }
        for domain, variables in state.domain_variables.items()
    }
    from dataclasses import replace

    pessimistic = replace(state, variables=docked, domain_variables=docked_domains)
    return _evaluate(parse(expression), pessimistic) >= 0.5
