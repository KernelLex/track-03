"""A small, restricted boolean-expression evaluator for check_bounds() rules. DEVDOC_v6 §13.

Rules are Python-syntax boolean expressions over a fixed context (debtor,
mandate, action, decision, invoice, comms, config, ...) so the same string
that ships in rules.yaml is directly executable — not a description of a
rule that some other code then hand-implements from scratch, which is
exactly the drift §13.4 and §21 are trying to prevent.

This is deliberately NOT eval(). It parses to an AST and a whitelist-only
validator rejects anything outside a small allowed set (no assignment, no
imports, no comprehensions, no attribute call chains beyond simple
`a.b.c`) before the expression is ever evaluated — because these strings
come from a YAML file, not from this module's own source.
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable

_ALLOWED_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_ALLOWED_COMPARES: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_ALLOWED_NODE_TYPES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Name,
    ast.Load, ast.Attribute, ast.Call, ast.Constant, ast.List, ast.Tuple, ast.Set,
)


class UnsafeExpression(Exception):
    """A rule uses syntax outside the whitelist, or references an unknown name.
    Always raised, never swallowed — a bounds rule that silently no-ops on bad
    syntax is worse than one that refuses to load (Law 3's whole point)."""


class BoundsExpr:
    """A compiled, validated rule expression. Parse once (at rules.yaml load
    time), evaluate many times against different contexts."""

    def __init__(self, source: str):
        self.source = source
        try:
            self._tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise UnsafeExpression(f"could not parse rule expression {source!r}: {exc}") from exc
        _Validator().visit(self._tree)

    def evaluate(self, namespace: dict[str, Any], functions: dict[str, Callable]) -> Any:
        return _Evaluator(namespace, functions).visit(self._tree.body)

    def __repr__(self) -> str:
        return f"BoundsExpr({self.source!r})"


class _Validator(ast.NodeVisitor):
    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise UnsafeExpression(f"disallowed syntax in bounds rule: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name):
            raise UnsafeExpression("bounds rules may only call a bare whitelisted function name")
        if node.keywords:
            raise UnsafeExpression("keyword arguments are not allowed in bounds rules")
        self.generic_visit(node)


class _Evaluator(ast.NodeVisitor):
    def __init__(self, namespace: dict[str, Any], functions: dict[str, Callable]):
        self.namespace = namespace
        self.functions = functions

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = self.visit(value_node)
                if not result:
                    return result
            return result
        result = False
        for value_node in node.values:
            result = self.visit(value_node)
            if result:
                return result
        return result

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        value = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not value
        if isinstance(node.op, ast.USub):
            return -value
        raise UnsafeExpression(f"unsupported unary operator {node.op!r}")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise UnsafeExpression(f"unsupported binary operator {node.op!r}")
        return op(self.visit(node.left), self.visit(node.right))

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        result = True
        for op_node, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            op = _ALLOWED_COMPARES.get(type(op_node))
            if op is None:
                raise UnsafeExpression(f"unsupported comparison operator {op_node!r}")
            result = result and op(left, right)
            left = right
        return result

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.namespace:
            return self.namespace[node.id]
        if node.id in self.functions:
            return self.functions[node.id]
        raise UnsafeExpression(f"unknown name {node.id!r} in bounds rule")

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        value = self.visit(node.value)
        try:
            return getattr(value, node.attr)
        except AttributeError as exc:
            raise UnsafeExpression(f"no attribute {node.attr!r} on {value!r}") from exc

    def visit_Call(self, node: ast.Call) -> Any:
        func = self.visit(node.func)
        args = [self.visit(a) for a in node.args]
        return func(*args)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_List(self, node: ast.List) -> Any:
        return [self.visit(e) for e in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(e) for e in node.elts)

    def visit_Set(self, node: ast.Set) -> Any:
        return {self.visit(e) for e in node.elts}

    def generic_visit(self, node: ast.AST) -> Any:
        raise UnsafeExpression(f"unsupported syntax during evaluation: {type(node).__name__}")
