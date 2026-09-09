"""Deterministic emitters: lowerings of the typed SQL AST into an executable target.

`engine/sql_ast.py` owns the IR — the node grammar, typing, and validation. This package owns the
lowerings of that IR: `emitter/sql/` renders SQL text, both for the own-data typed AST and for the
composition view stack.

Layering rule: this package imports from `engine`, and `engine/sql_ast.py` must never import
`deterministic`, so the IR stays the single definition of meaning that every target renders.
"""
