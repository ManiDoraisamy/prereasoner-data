"""Python emission: a TEST-ONLY lowering of the typed AST, used as a differential oracle.

Nothing under this package may be imported by serving code. It exists so a second, independently
written implementation can recompute what the SQL path computed and be required to agree, row for
row. Determinism says the same question yields the same SQL; it does not say that SQL is correct,
and until this package existed nothing recomputed the relational algebra independently.

Two rules keep the oracle honest, both enforced by `tests/test_emitter_parity.py`:

* It must not re-ask the database. The ORM may only materialize whole tables; joins, filters,
  grouping and ordering happen here, in Python. Otherwise this is SQL checking SQL.
* It is written from AST semantics, never transliterated from `emitter/sql/render.py`. A copy of
  the renderer would reproduce the renderer's bugs and prove nothing.

Arithmetic is the deliberate exception to "written independently": `engine/numeric.py` is the one
kernel both emitters use, so they agree on arithmetic by construction and the oracle is left
testing relational algebra, which is where the interesting bugs are.
"""
