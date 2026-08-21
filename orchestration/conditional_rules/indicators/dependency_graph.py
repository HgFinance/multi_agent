"""Shared indicator dependency collection for one rule evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..contracts import ExpressionNode, ExpressionType


@dataclass(frozen=True)
class IndicatorDependencyGraph:
    nodes_by_key: dict[str, ExpressionNode]

    @property
    def nodes(self) -> tuple[ExpressionNode, ...]:
        return tuple(self.nodes_by_key[key] for key in sorted(self.nodes_by_key))

    def for_timeframe(self, timeframe: str) -> tuple[ExpressionNode, ...]:
        return tuple(
            node
            for node in self.nodes
            if node.timeframe is not None and node.timeframe.value == timeframe
        )


def build_dependency_graph(
    root: ExpressionNode,
    *,
    market_data_source_id: str | None = None,
    key_fn: Callable[[ExpressionNode], str] | None = None,
) -> IndicatorDependencyGraph:
    if key_fn is None:
        from ..evaluator import indicator_key

        key_fn = lambda node: indicator_key(
            node, market_data_source_id=market_data_source_id
        )
    found: dict[str, ExpressionNode] = {}

    def visit(node: ExpressionNode | None) -> None:
        if node is None:
            return
        if node.type is ExpressionType.INDICATOR:
            found[key_fn(node)] = node
        visit(node.left)
        visit(node.right)
        visit(node.operand)
        for child in node.children or ():
            visit(child)

    visit(root)
    return IndicatorDependencyGraph(found)
