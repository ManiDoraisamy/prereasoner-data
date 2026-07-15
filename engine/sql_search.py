"""Deterministic search over typed SQL ASTs.

The searcher does not decode SQL tokens.  It links question spans to typed schema
objects, expands a bounded beam of semantic choices, searches the FK graph for
join trees (including bridge tables), validates complete ASTs, and only then
renders SQL. Phase 2 applies the inspectable semantic ranker; Phase 3 adds a
bounded recursive-query expansion; Phase 4 adds aggregate constraints,
disjunctions, and relational subqueries; Phase 5 adds deterministic arg-extrema,
top-N, and set-difference candidates before ranking.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import heapq
import re
from typing import Any, Iterable, Sequence

from engine.sql_ast import (
    Aggregate,
    ColumnRef,
    Comparison,
    Join,
    Literal,
    OrderTerm,
    Query,
    SQLType,
    SelectItem,
    SelectQuery,
    Star,
    and_predicates,
    render_query,
    validate_query,
)


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")
_NUMBER_RE = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T].*)?$")
_PROJECTION_CUES = frozenset({"show", "list", "display", "select", "give", "find", "which", "what"})
_ID_WORDS = frozenset({"id", "identifier", "code", "key"})
_NAME_WORDS = frozenset({"name", "title", "label"})


@dataclass(frozen=True)
class SchemaColumn:
    ref: ColumnRef
    values: tuple[Any, ...] = ()
    index: int = -1


@dataclass(frozen=True)
class ForeignKey:
    from_column: ColumnRef
    to_column: ColumnRef
    confidence: float = 1.0

    @property
    def tables(self) -> frozenset[str]:
        return frozenset((self.from_column.table, self.to_column.table))

    @property
    def signature(self) -> tuple[str, str, str, str]:
        return (self.from_column.table, self.from_column.name,
                self.to_column.table, self.to_column.name)


@dataclass(frozen=True)
class JoinTree:
    root: str
    edge_indexes: tuple[int, ...]
    joins: tuple[Join, ...]
    confidence: float


class SchemaGraph:
    def __init__(self, columns: Iterable[SchemaColumn], foreign_keys: Iterable[ForeignKey]):
        self.columns = tuple(columns)
        self.by_table: dict[str, tuple[SchemaColumn, ...]] = {}
        grouped: dict[str, list[SchemaColumn]] = {}
        for column in self.columns:
            grouped.setdefault(column.ref.table, []).append(column)
        self.by_table = {table: tuple(cols) for table, cols in grouped.items()}
        self.tables = tuple(grouped)
        self.column_map = {(c.ref.table, c.ref.name): c for c in self.columns}
        self.foreign_keys = tuple(
            fk for fk in foreign_keys
            if fk.from_column.table in self.by_table and fk.to_column.table in self.by_table
        )
        adjacency: dict[str, list[int]] = {table: [] for table in self.tables}
        for i, fk in enumerate(self.foreign_keys):
            adjacency[fk.from_column.table].append(i)
            adjacency[fk.to_column.table].append(i)
        self.adjacency = {table: tuple(sorted(indexes, key=self._edge_sort_key))
                          for table, indexes in adjacency.items()}
        self.value_index = self._build_value_index()

    @classmethod
    def from_tables(cls, tables: Sequence[dict], fks: Sequence[dict | tuple]) -> "SchemaGraph":
        columns: list[SchemaColumn] = []
        refs: dict[tuple[str, str], ColumnRef] = {}
        index = 0
        for table in tables:
            name = str(table["name"])
            names = [str(c) for c in table["columns"]]
            rows = table.get("rows") or []
            for ci, col_name in enumerate(names):
                values = tuple(_row_value(row, names, ci, col_name) for row in rows)
                ref = ColumnRef(name, col_name, _infer_type(col_name, values))
                refs[(name, col_name)] = ref
                columns.append(SchemaColumn(ref, values, index))
                index += 1
        edges = [_foreign_key(fk, refs) for fk in fks]
        return cls(columns, (edge for edge in edges if edge is not None))

    @classmethod
    def from_planner(cls, sch: Sequence[dict], fks: Sequence[dict | tuple]) -> "SchemaGraph":
        columns: list[SchemaColumn] = []
        refs: dict[tuple[str, str], ColumnRef] = {}
        for i, col in enumerate(sch):
            typ = _planner_type(col)
            ref = ColumnRef(str(col["table"]), str(col["name"]), typ)
            refs[(ref.table, ref.name)] = ref
            columns.append(SchemaColumn(ref, tuple(col.get("values") or ()), int(col.get("idx", i))))
        edges = [_foreign_key(fk, refs) for fk in fks]
        return cls(columns, (edge for edge in edges if edge is not None))

    def display_columns(self, table: str) -> tuple[ColumnRef, ...]:
        columns = list(self.by_table.get(table, ()))
        columns.sort(key=lambda c: (
            0 if set(_name_words(c.ref.name)) & _NAME_WORDS else 1,
            0 if c.ref.type == SQLType.TEXT else 1,
            1 if _is_id(c.ref.name) else 0,
            c.index,
        ))
        return tuple(c.ref for c in columns)

    def join_trees(self, required_tables: Iterable[str], preferred_root: str | None = None,
                   limit: int = 8, max_hops: int = 6) -> tuple[JoinTree, ...]:
        required = frozenset(t for t in required_tables if t in self.by_table)
        if not required:
            return ()
        root = preferred_root if preferred_root in required else sorted(required)[0]
        if len(required) == 1:
            return (JoinTree(root, (), (), 1.0),)

        queue: list[tuple[int, float, tuple, frozenset[str], tuple[int, ...]]] = []
        heapq.heappush(queue, (0, 0.0, (), frozenset((root,)), ()))
        seen: set[tuple[frozenset[str], tuple[int, ...]]] = set()
        found: list[JoinTree] = []
        expansions = 0
        while queue and len(found) < limit and expansions < 5000:
            _, _, _, nodes, edges = heapq.heappop(queue)
            state_key = (nodes, edges)
            if state_key in seen:
                continue
            seen.add(state_key)
            expansions += 1
            if required <= nodes:
                joins = self._materialize_joins(root, edges)
                confidence = sum(self.foreign_keys[i].confidence for i in edges) / max(len(edges), 1)
                found.append(JoinTree(root, edges, joins, confidence))
                continue
            if len(edges) >= max_hops:
                continue
            candidates = sorted({i for table in nodes for i in self.adjacency.get(table, ())},
                                key=self._edge_sort_key)
            for edge_index in candidates:
                fk = self.foreign_keys[edge_index]
                outside = fk.tables - nodes
                if len(outside) != 1:
                    continue
                new_edges = tuple(sorted(edges + (edge_index,)))
                new_nodes = nodes | outside
                signature = tuple(self.foreign_keys[i].signature for i in new_edges)
                conf_cost = -sum(self.foreign_keys[i].confidence for i in new_edges)
                heapq.heappush(queue, (len(new_edges), conf_cost, signature, new_nodes, new_edges))
        return tuple(found)

    def _materialize_joins(self, root: str, edge_indexes: tuple[int, ...]) -> tuple[Join, ...]:
        remaining = list(edge_indexes)
        joined = {root}
        out: list[Join] = []
        while remaining:
            picked = None
            for edge_index in sorted(remaining, key=self._edge_sort_key):
                fk = self.foreign_keys[edge_index]
                left_in = fk.from_column.table in joined
                right_in = fk.to_column.table in joined
                if left_in == right_in:
                    continue
                new_table = fk.to_column.table if left_in else fk.from_column.table
                out.append(Join(new_table, fk.from_column, fk.to_column))
                joined.add(new_table)
                picked = edge_index
                break
            if picked is None:
                raise ValueError("join edge set is disconnected")
            remaining.remove(picked)
        return tuple(out)

    def _edge_sort_key(self, edge_index: int) -> tuple:
        edge = self.foreign_keys[edge_index]
        return (-edge.confidence, edge.signature)

    def _build_value_index(self) -> dict[str, tuple[tuple[ColumnRef, Any], ...]]:
        values: dict[str, list[tuple[ColumnRef, Any]]] = {}
        for column in self.columns:
            seen = set()
            for value in column.values:
                norm = _normal_value(value)
                if not norm or norm in seen or _NUMBER_RE.match(norm):
                    continue
                seen.add(norm)
                values.setdefault(norm, []).append((column.ref, value))
        return {value: tuple(options) for value, options in values.items()}


@dataclass(frozen=True)
class ScoredQuery:
    query: Query
    sql: str
    score: float
    evidence: tuple[str, ...]
    features: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class _ColumnOption:
    column: ColumnRef
    score: float
    position: int


@dataclass(frozen=True)
class _Mention:
    position: int
    options: tuple[_ColumnOption, ...]


@dataclass(frozen=True)
class _Draft:
    projections: tuple[ColumnRef, ...] = ()
    aggregates: tuple[Aggregate, ...] = ()
    predicates: tuple[Comparison, ...] = ()
    score: float = 0.0
    evidence: tuple[str, ...] = ()


class SQLSearcher:
    def __init__(self, schema: SchemaGraph, beam_size: int = 64, max_candidates: int = 25):
        if not schema.tables:
            raise ValueError("SQL search requires at least one table")
        self.schema = schema
        self.beam_size = max(1, beam_size)
        self.max_candidates = max(1, max_candidates)

    @classmethod
    def from_tables(cls, tables: Sequence[dict], fks: Sequence[dict | tuple], **kwargs) -> "SQLSearcher":
        return cls(SchemaGraph.from_tables(tables, fks), **kwargs)

    def search(self, question: str, semantic_signals=None, phase2: bool = True,
               phase3: bool = True, phase4: bool = True,
               phase5: bool = True) -> list[ScoredQuery]:
        tokens = _tokens(question)
        if not tokens:
            return []
        table_scores = self._table_scores(tokens)
        mentions = self._column_mentions(tokens, table_scores)
        clause_boundary = next((i for i, token in enumerate(tokens)
                                if token in {"where", "with", "whose", "having", "from", "for",
                                             "order", "ordered", "sort", "sorted", "rank", "ranked"}),
                               len(tokens))
        prefix_tokens = set(tokens[:clause_boundary])
        explicit_projection_columns = set()
        for schema_column in self.schema.columns:
            name_tokens = {_canon(word) for word in _name_words(schema_column.ref.name) if _canon(word) != "id"}
            if name_tokens and name_tokens <= prefix_tokens:
                explicit_projection_columns.add(schema_column.ref)
        clause_only_columns = {
            option.column for mention in mentions for option in mention.options
            if option.column not in explicit_projection_columns
        }

        projection_choices = self._projection_choices(tokens, mentions, table_scores)
        aggregate_choices = self._aggregate_choices(tokens, mentions)
        predicate_choices = self._predicate_choices(tokens, mentions)

        drafts = [_Draft()]
        drafts = self._expand(drafts, projection_choices, "projections")
        drafts = self._expand(drafts, aggregate_choices, "aggregates")
        drafts = self._expand(drafts, predicate_choices, "predicates")

        complete: list[ScoredQuery] = []
        for draft in drafts:
            groups = self._group_choices(tokens, mentions, table_scores, draft)
            for group_columns, group_score, group_evidence in groups:
                raw_projection = tuple(c for c in draft.projections
                                       if c not in {a.operand for a in draft.aggregates
                                                    if isinstance(a.operand, ColumnRef)})
                if not draft.aggregates and len(raw_projection) > 1:
                    predicate_columns = {predicate.left for predicate in draft.predicates
                                         if isinstance(predicate.left, ColumnRef)}
                    raw_projection = tuple(c for c in raw_projection
                                           if c in explicit_projection_columns
                                           or (c not in predicate_columns and c not in clause_only_columns))
                if draft.aggregates:
                    grouped = _unique_columns(group_columns + raw_projection)
                    expressions = tuple(SelectItem(c) for c in grouped) + tuple(
                        SelectItem(a) for a in draft.aggregates
                    )
                else:
                    grouped = ()
                    expressions = tuple(SelectItem(c) for c in raw_projection) or (SelectItem(Star()),)

                orders = self._order_choices(tokens, mentions, draft)
                for order_terms, limit, order_score, order_evidence in orders:
                    required = self._required_tables(expressions, draft.predicates, grouped, order_terms)
                    mentioned_tables = {table for table, score in table_scores.items() if score >= 2.5}
                    required.update(mentioned_tables)
                    if not required:
                        required.add(max(table_scores, key=table_scores.get) if table_scores else self.schema.tables[0])
                    root = self._preferred_root(required, table_scores, draft)
                    for tree in self.schema.join_trees(required, root):
                        query = SelectQuery(
                            select=expressions,
                            from_table=tree.root,
                            joins=tree.joins,
                            where=and_predicates(draft.predicates),
                            group_by=grouped,
                            order_by=order_terms,
                            limit=limit,
                            distinct=bool({"distinct", "different", "unique"} & set(tokens)) and not draft.aggregates,
                        )
                        try:
                            validate_query(query)
                            sql = render_query(query)
                        except (TypeError, ValueError):
                            continue
                        join_score = -0.2 * len(tree.joins) + 0.1 * tree.confidence
                        evidence = (draft.evidence + group_evidence + order_evidence
                                    + tuple(f"join:{self.schema.foreign_keys[i].signature}" for i in tree.edge_indexes))
                        complete.append(ScoredQuery(query, sql,
                                                    draft.score + group_score + order_score + join_score,
                                                    evidence))

        dedup: dict[str, ScoredQuery] = {}
        for candidate in complete:
            old = dedup.get(candidate.sql)
            if old is None or candidate.score > old.score:
                dedup[candidate.sql] = candidate
        pool_size = max(self.beam_size, self.max_candidates * 4)
        base = sorted(dedup.values(), key=lambda c: (-c.score, c.sql))[:pool_size]
        pool = base
        if phase3:
            from engine.sql_phase3 import Phase3Expander
            advanced = Phase3Expander(self.schema, pool_size).expand(question, base)
            combined = {candidate.sql: candidate for candidate in base}
            for candidate in advanced:
                old = combined.get(candidate.sql)
                if old is None or candidate.score > old.score:
                    combined[candidate.sql] = candidate
            pool = sorted(combined.values(), key=lambda candidate: (-candidate.score, candidate.sql))
        if phase4:
            from engine.sql_phase4 import Phase4Expander
            advanced = Phase4Expander(self.schema, pool_size).expand(question, pool)
            combined = {candidate.sql: candidate for candidate in pool}
            for candidate in advanced:
                old = combined.get(candidate.sql)
                if old is None or candidate.score > old.score:
                    combined[candidate.sql] = candidate
            pool = sorted(combined.values(), key=lambda candidate: (-candidate.score, candidate.sql))
        if phase5:
            from engine.sql_phase5 import Phase5Expander
            advanced = Phase5Expander(self.schema, pool_size).expand(question, pool)
            combined = {candidate.sql: candidate for candidate in pool}
            for candidate in advanced:
                old = combined.get(candidate.sql)
                if old is None or candidate.score > old.score:
                    combined[candidate.sql] = candidate
            pool = sorted(combined.values(), key=lambda candidate: (-candidate.score, candidate.sql))
        if not phase2:
            return pool[:self.max_candidates]
        from engine.sql_rank import CandidateRanker
        return CandidateRanker(self.schema, semantic_signals).rank(question, pool)[:self.max_candidates]

    def _expand(self, drafts: list[_Draft], choices: list[tuple[tuple, float, tuple[str, ...]]],
                field: str) -> list[_Draft]:
        expanded = []
        for draft in drafts:
            for value, score, evidence in choices:
                expanded.append(replace(draft, **{field: value}, score=draft.score + score,
                                        evidence=draft.evidence + evidence))
        return sorted(expanded, key=lambda d: (-d.score, repr(d)))[:self.beam_size]

    def _table_scores(self, tokens: tuple[str, ...]) -> dict[str, float]:
        scores = {}
        token_set = set(tokens)
        for table in self.schema.tables:
            words = _name_words(table)
            coverage = sum(1 for word in words if _canon(word) in token_set)
            plural = any(_canon(token) == _canon(word) for token in tokens for word in words)
            scores[table] = (3.0 if coverage == len(words) and words else 0.0) + (1.0 if plural else 0.0)
        return scores

    def _column_mentions(self, tokens: tuple[str, ...], table_scores: dict[str, float]) -> tuple[_Mention, ...]:
        grouped: dict[int, list[_ColumnOption]] = {}
        token_set = set(tokens)
        id_requested = bool(_ID_WORDS & token_set)
        for schema_column in self.schema.columns:
            column = schema_column.ref
            words = tuple(_canon(w) for w in _name_words(column.name))
            meaningful = tuple(w for w in words if w != "id")
            if _is_id(column.name) and not id_requested:
                continue
            positions = [i for i, token in enumerate(tokens) if token in meaningful]
            if not positions:
                continue
            coverage = len({tokens[i] for i in positions} & set(meaningful)) / max(len(set(meaningful)), 1)
            if coverage < 1.0 and len(meaningful) > 1:
                continue
            position = max(positions)
            phrase = " ".join(meaningful)
            exact = phrase in " ".join(tokens)
            score = 3.0 + coverage + (1.0 if exact else 0.0) + 0.15 * table_scores.get(column.table, 0.0)
            grouped.setdefault(position, []).append(_ColumnOption(column, score, position))
        mentions = []
        for position, options in sorted(grouped.items()):
            options.sort(key=lambda option: (-option.score, option.column.table, option.column.name))
            mentions.append(_Mention(position, tuple(options[:4])))
        return tuple(mentions)

    def _projection_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                            table_scores: dict[str, float]) -> list[tuple[tuple, float, tuple[str, ...]]]:
        if mentions:
            beam: list[tuple[tuple[ColumnRef, ...], float, tuple[str, ...]]] = [((), 0.0, ())]
            for mention in mentions:
                expanded = []
                for columns, score, evidence in beam:
                    for option in mention.options:
                        chosen = _unique_columns(columns + (option.column,))
                        expanded.append((chosen, score + option.score,
                                         evidence + (f"column:{option.column.table}.{option.column.name}",)))
                beam = sorted(expanded, key=lambda item: (-item[1], repr(item[0])))[:self.beam_size]
            return [(columns, score, evidence) for columns, score, evidence in beam]

        if _PROJECTION_CUES & set(tokens):
            table_options = sorted(table_scores.items(), key=lambda item: (-item[1], item[0]))
            out = []
            for table, score in table_options[:3]:
                displays = self.schema.display_columns(table)
                if displays:
                    out.append(((displays[0],), max(score, 0.5), (f"entity-display:{table}.{displays[0].name}",)))
            if out:
                return out
        return [((), 0.0, ())]

    def _aggregate_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...]) -> list[tuple[tuple, float, tuple[str, ...]]]:
        cues: list[tuple[str, int]] = []
        for i, token in enumerate(tokens):
            if token in {"count", "counts"} or (token == "number" and (i + 1 >= len(tokens) or tokens[i + 1] == "of")):
                cues.append(("COUNT", i))
            elif token in {"sum", "total"}:
                if token == "total" and i + 1 < len(tokens) and tokens[i + 1] in {"number", "count"}:
                    continue
                cues.append(("SUM", i))
            elif token in {"average", "avg", "mean"}:
                cues.append(("AVG", i))
            elif token in {"minimum", "min"}:
                cues.append(("MIN", i))
            elif token in {"maximum", "max"}:
                cues.append(("MAX", i))
        for i in range(len(tokens) - 1):
            if tokens[i:i + 2] == ("how", "many"):
                cues.append(("COUNT", i))
        cues = list(dict.fromkeys(cues))
        # "count the number" and "average mean" are reinforcing paraphrases, not requests for duplicate
        # SELECT items.  Phase 1 supports several *different* aggregates in one query; repeated functions
        # collapse to their earliest cue until argument-scope parsing arrives in a later phase.
        seen_functions = set()
        unique_cues = []
        for function, position in sorted(cues, key=lambda item: item[1]):
            if function not in seen_functions:
                unique_cues.append((function, position))
                seen_functions.add(function)
        cues = unique_cues
        if not cues:
            return [((), 0.0, ())]

        beam: list[tuple[tuple[Aggregate, ...], float, tuple[str, ...]]] = [((), 0.0, ())]
        for function, position in sorted(cues, key=lambda item: item[1]):
            options: list[tuple[Aggregate, float, str]] = []
            if function == "COUNT":
                options.append((Aggregate("COUNT", Star()), 4.0, "aggregate:COUNT(*)"))
                for option in self._target_columns(mentions, position, numeric=False)[:3]:
                    distinct = bool({"different", "distinct", "unique"} & set(tokens))
                    options.append((Aggregate("COUNT", option.column, distinct), 3.3 + option.score * 0.1,
                                    f"aggregate:COUNT({option.column.table}.{option.column.name})"))
            else:
                targets = self._target_columns(mentions, position, numeric=function in {"SUM", "AVG"})
                if not targets:
                    targets = [
                        _ColumnOption(c.ref, 0.0, len(tokens)) for c in self.schema.columns
                        if c.ref.type.numeric and not _is_id(c.ref.name)
                    ][:4]
                for option in targets[:4]:
                    if function in {"SUM", "AVG"} and not option.column.type.numeric:
                        continue
                    options.append((Aggregate(function, option.column), 4.0 + option.score * 0.1,
                                    f"aggregate:{function}({option.column.table}.{option.column.name})"))
            expanded = []
            for aggregates, score, evidence in beam:
                for aggregate, option_score, reason in options:
                    if aggregate in aggregates:
                        continue
                    expanded.append((aggregates + (aggregate,), score + option_score, evidence + (reason,)))
            beam = sorted(expanded, key=lambda item: (-item[1], repr(item[0])))[:self.beam_size]
        return [(aggregates, score, evidence) for aggregates, score, evidence in beam]

    def _target_columns(self, mentions: tuple[_Mention, ...], position: int, numeric: bool) -> list[_ColumnOption]:
        options = [option for mention in mentions for option in mention.options
                   if not numeric or option.column.type.numeric]
        options.sort(key=lambda option: (
            0 if option.position >= position else 1,
            abs(option.position - position),
            -option.score,
            option.column.table,
            option.column.name,
        ))
        return options

    def _predicate_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...]) -> list[tuple[tuple, float, tuple[str, ...]]]:
        groups: list[list[tuple[tuple[Comparison, ...], float, str]]] = []
        groups.extend(self._value_predicate_groups(tokens))
        groups.extend(self._numeric_predicate_groups(tokens, mentions))
        if not groups:
            return [((), 0.0, ())]
        beam: list[tuple[tuple[Comparison, ...], float, tuple[str, ...]]] = [((), 0.0, ())]
        for group in groups:
            expanded = []
            for predicates, score, evidence in beam:
                for additions, option_score, reason in group:
                    expanded.append((predicates + additions, score + option_score, evidence + (reason,)))
            beam = sorted(expanded, key=lambda item: (-item[1], repr(item[0])))[:self.beam_size]
        return [(predicates, score, evidence) for predicates, score, evidence in beam]

    def _value_predicate_groups(self, tokens: tuple[str, ...]) -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        matches: list[tuple[int, int, str, tuple[tuple[ColumnRef, Any], ...]]] = []
        for start in range(len(tokens)):
            for size in range(1, min(6, len(tokens) - start) + 1):
                phrase = " ".join(tokens[start:start + size])
                options = self.schema.value_index.get(phrase)
                if options:
                    matches.append((start, start + size, phrase, options))
        matches.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
        occupied: set[int] = set()
        selected = []
        for match in matches:
            span = set(range(match[0], match[1]))
            if span & occupied:
                continue
            occupied.update(span)
            selected.append(match)
        selected.sort()
        groups = []
        for start, _, phrase, options in selected:
            operator = "!=" if set(tokens[max(0, start - 3):start]) & {"not", "except", "excluding", "without"} else "="
            choices = []
            for column, value in sorted(options, key=lambda item: (item[0].table, item[0].name))[:4]:
                literal = Literal(value, column.type)
                choices.append(((Comparison(column, operator, literal),), 5.0,
                                f"value:{column.table}.{column.name}{operator}{phrase}"))
            groups.append(choices)
        return groups

    def _numeric_predicate_groups(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...]) -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        groups = []
        used_numbers: set[int] = set()
        for i, token in enumerate(tokens):
            if token != "between":
                continue
            found = [(j, _number(tokens[j])) for j in range(i + 1, min(len(tokens), i + 7)) if _NUMBER_RE.match(tokens[j])]
            if len(found) < 2:
                continue
            targets = self._numeric_targets(mentions, i)
            options = []
            for target in targets[:4]:
                lo_i, lo = found[0]; hi_i, hi = found[1]
                used_numbers.update((lo_i, hi_i))
                options.append(((Comparison(target, ">=", Literal(lo, target.type)),
                                 Comparison(target, "<=", Literal(hi, target.type))), 5.0,
                                f"between:{target.table}.{target.name}"))
            if options:
                groups.append(options)

        patterns = [
            (("at", "least"), ">="), (("at", "most"), "<="), (("more", "than"), ">"),
            (("greater", "than"), ">"), (("less", "than"), "<"), (("fewer", "than"), "<"),
            (("over",), ">"), (("above",), ">"), (("under",), "<"), (("below",), "<"),
            (("after",), ">"), (("before",), "<"), (("since",), ">="),
            (("equal", "to"), "="), (("equals",), "="), (("exactly",), "="),
        ]
        for cue, operator in patterns:
            size = len(cue)
            for i in range(len(tokens) - size + 1):
                if tokens[i:i + size] != cue:
                    continue
                number_index = next((j for j in range(i + size, min(len(tokens), i + size + 5))
                                     if j not in used_numbers and _NUMBER_RE.match(tokens[j])), None)
                if number_index is None:
                    continue
                targets = self._numeric_targets(mentions, i)
                value = _number(tokens[number_index])
                options = [((Comparison(target, operator, Literal(value, target.type)),), 4.5,
                            f"comparison:{target.table}.{target.name}{operator}{value}")
                           for target in targets[:4]]
                if options:
                    groups.append(options)
                    used_numbers.add(number_index)

        for i, token in enumerate(tokens):
            if i in used_numbers or not re.fullmatch(r"(?:19|20)\d{2}", token):
                continue
            year_columns = [c.ref for c in self.schema.columns
                            if c.ref.type == SQLType.DATE or "year" in _name_words(c.ref.name)]
            if not year_columns:
                continue
            options = []
            for column in year_columns[:4]:
                if column.type == SQLType.DATE:
                    start, end = f"{token}-01-01", f"{int(token) + 1}-01-01"
                    options.append(((Comparison(column, ">=", Literal(start, SQLType.DATE)),
                                     Comparison(column, "<", Literal(end, SQLType.DATE))), 4.0,
                                    f"year:{column.table}.{column.name}={token}"))
                else:
                    options.append(((Comparison(column, "=", Literal(int(token), column.type)),), 4.0,
                                    f"year:{column.table}.{column.name}={token}"))
            if options:
                groups.append(options)
        return groups

    def _numeric_targets(self, mentions: tuple[_Mention, ...], position: int) -> list[ColumnRef]:
        options = [option for mention in mentions for option in mention.options if option.column.type.numeric]
        options.sort(key=lambda option: (abs(option.position - position), -option.score,
                                         option.column.table, option.column.name))
        refs = _unique_columns(tuple(option.column for option in options))
        if refs:
            return list(refs)
        return [c.ref for c in self.schema.columns if c.ref.type.numeric and not _is_id(c.ref.name)]

    def _group_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                       table_scores: dict[str, float], draft: _Draft) -> list[tuple[tuple[ColumnRef, ...], float, tuple[str, ...]]]:
        if not draft.aggregates:
            return [((), 0.0, ())]
        targets = {a.operand for a in draft.aggregates if isinstance(a.operand, ColumnRef)}
        projection_groups = _unique_columns(tuple(c for c in draft.projections if c not in targets))
        explicit_positions = []
        for i, token in enumerate(tokens):
            if token in {"each", "per"} or (token == "by" and i > 0):
                explicit_positions.append(i)
        options: list[tuple[tuple[ColumnRef, ...], float, tuple[str, ...]]] = []
        for position in explicit_positions:
            nearby = sorted(
                (option for mention in mentions for option in mention.options
                 if option.column not in targets),
                key=lambda option: (0 if option.position >= position else 1,
                                    abs(option.position - position), -option.score,
                                    option.column.table, option.column.name),
            )
            for option in nearby[:3]:
                groups = _unique_columns(projection_groups + (option.column,))
                options.append((groups, 3.0 + option.score * 0.1,
                                (f"group:{option.column.table}.{option.column.name}",)))
            if tokens[position] in {"each", "per"} or not projection_groups:
                table_positions = {
                    table: min((i for i, token in enumerate(tokens)
                                if token in {_canon(w) for w in _name_words(table)}), default=len(tokens) + 5)
                    for table in self.schema.tables
                }
                for table, score in sorted(table_scores.items(), key=lambda item: (-item[1], item[0])):
                    if score <= 0:
                        continue
                    displays = self.schema.display_columns(table)
                    if displays:
                        groups = _unique_columns(projection_groups + (displays[0],))
                        distance = abs(table_positions[table] - position)
                        position_bonus = 1.5 if table_positions[table] >= position else 0.0
                        options.append((groups, 2.8 + score * 0.1 + position_bonus - 0.05 * distance,
                                        (f"group-entity:{table}.{displays[0].name}",)))
        if projection_groups:
            options.append((projection_groups, 2.5, tuple(f"group:{c.table}.{c.name}" for c in projection_groups)))
        if not options:
            options.append(((), 0.0, ()))
        dedup = {}
        for choice in options:
            old = dedup.get(choice[0])
            if old is None or choice[1] > old[1]:
                dedup[choice[0]] = choice
        return sorted(dedup.values(), key=lambda item: (-item[1], repr(item[0])))[:8]

    def _order_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                       draft: _Draft) -> list[tuple[tuple[OrderTerm, ...], int | None, float, tuple[str, ...]]]:
        token_set = set(tokens)
        direction = None
        if token_set & {"descending", "desc", "highest", "largest", "biggest", "most", "latest", "newest", "top"}:
            direction = "DESC"
        elif token_set & {"ascending", "asc", "lowest", "smallest", "least", "earliest", "oldest", "bottom"}:
            direction = "ASC"

        limit = None
        for i, token in enumerate(tokens):
            if token in {"top", "bottom", "first"}:
                limit = next((int(_number(tokens[j])) for j in range(i + 1, min(len(tokens), i + 4))
                              if _NUMBER_RE.match(tokens[j]) and int(_number(tokens[j])) > 0), None)
                limit = limit or 1
                break
        if limit is None and token_set & {"most", "least", "highest", "lowest", "largest", "smallest"}:
            limit = 1

        order_cue = direction is not None or limit is not None or bool(token_set & {"order", "ordered", "sort", "sorted", "rank", "ranked"})
        if not order_cue:
            return [((), None, 0.0, ())]
        direction = direction or ("DESC" if draft.aggregates else "ASC")

        expressions: list[tuple[ColumnRef | Aggregate, float]] = []
        by_position = next((i for i, token in enumerate(tokens) if token == "by"), None)
        if by_position is not None:
            nearby = self._target_columns(mentions, by_position, numeric=False)
            expressions.extend((option.column, 2.0 - 0.1 * abs(option.position - by_position))
                               for option in nearby[:4])
        if draft.aggregates:
            aggregate_bonus = 3.0 if (by_position is not None or limit is not None) else 1.0
            expressions = [(aggregate, aggregate_bonus) for aggregate in draft.aggregates] + expressions
        if not expressions:
            typed = [c.ref for c in self.schema.columns
                     if c.ref.type in {SQLType.INTEGER, SQLType.REAL, SQLType.DATE} and not _is_id(c.ref.name)]
            expressions.extend((column, 0.5) for column in typed[:4])
        out = []
        seen = set()
        for expression, expression_score in expressions:
            if expression in seen:
                continue
            seen.add(expression)
            out.append(((OrderTerm(expression, direction),), limit, 3.0 + expression_score,
                        (f"order:{_expr_label(expression)}:{direction}",)))
        return sorted(out, key=lambda item: (-item[2], repr(item[0])))[:6] or [
            ((), limit, 0.5, (f"limit:{limit}",) if limit else ())
        ]

    @staticmethod
    def _required_tables(select: tuple[SelectItem, ...], predicates: tuple[Comparison, ...],
                         groups: tuple[ColumnRef, ...], orders: tuple[OrderTerm, ...]) -> set[str]:
        out = set()
        for item in select:
            out.update(_expression_tables(item.expression))
        for predicate in predicates:
            out.update(_expression_tables(predicate.left))
            out.update(_expression_tables(predicate.right))
        out.update(c.table for c in groups)
        for order in orders:
            out.update(_expression_tables(order.expression))
        return out

    @staticmethod
    def _preferred_root(required: set[str], table_scores: dict[str, float], draft: _Draft) -> str:
        count_tables = [a.operand.table for a in draft.aggregates if isinstance(a.operand, ColumnRef)]
        if count_tables:
            return count_tables[0]
        return sorted(required, key=lambda table: (-table_scores.get(table, 0.0), table))[0]


def _foreign_key(raw: dict | tuple, refs: dict[tuple[str, str], ColumnRef]) -> ForeignKey | None:
    if isinstance(raw, dict):
        ft, fc = str(raw["from_table"]), str(raw["from_col"])
        tt, tc = str(raw["to_table"]), str(raw["to_col"])
        confidence = float(raw.get("conf", raw.get("confidence", 1.0)) or 1.0)
    else:
        ft, fc, tt, tc = map(str, raw[:4])
        confidence = float(raw[4]) if len(raw) > 4 else 1.0
    left, right = refs.get((ft, fc)), refs.get((tt, tc))
    return ForeignKey(left, right, confidence) if left is not None and right is not None else None


def _planner_type(column: dict) -> SQLType:
    if column.get("is_date"):
        return SQLType.DATE
    affinity = str(column.get("affinity", "")).upper()
    return {"INTEGER": SQLType.INTEGER, "REAL": SQLType.REAL, "NUMERIC": SQLType.REAL,
            "BOOLEAN": SQLType.BOOLEAN, "DATE": SQLType.DATE, "TEXT": SQLType.TEXT}.get(affinity, SQLType.UNKNOWN)


def _infer_type(name: str, values: Sequence[Any]) -> SQLType:
    populated = [value for value in values if value is not None and str(value).strip()]
    if populated and all(_DATE_RE.match(str(value).strip()) for value in populated):
        return SQLType.DATE
    if populated and all(_NUMBER_RE.match(str(value).strip()) for value in populated):
        return SQLType.REAL if any("." in str(value) for value in populated) else SQLType.INTEGER
    if set(_name_words(name)) & {"date", "datetime", "timestamp"}:
        return SQLType.DATE
    return SQLType.TEXT


def _row_value(row: Any, names: list[str], index: int, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return row[index] if index < len(row) else None


def _tokens(question: str) -> tuple[str, ...]:
    return tuple(_canon(token) for token in _WORD_RE.findall(question.lower()))


def _name_words(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(word.lower() for word in re.findall(r"[A-Za-z0-9]+", spaced))


def _canon(word: str) -> str:
    word = word.lower().strip()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _is_id(name: str) -> bool:
    words = _name_words(name)
    return bool(words) and words[-1].lower() in {"id", "identifier", "key"}


def _normal_value(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(_tokens(str(value)))


def _number(value: str) -> int | float:
    cleaned = value.replace(",", "")
    return float(cleaned) if "." in cleaned else int(cleaned)


def _unique_columns(columns: tuple[ColumnRef, ...]) -> tuple[ColumnRef, ...]:
    return tuple(dict.fromkeys(columns))


def _expression_tables(expression: Any) -> set[str]:
    if isinstance(expression, ColumnRef):
        return {expression.table}
    if isinstance(expression, Aggregate):
        return _expression_tables(expression.operand)
    return set()


def _expr_label(expression: ColumnRef | Aggregate) -> str:
    if isinstance(expression, ColumnRef):
        return f"{expression.table}.{expression.name}"
    operand = "*" if isinstance(expression.operand, Star) else f"{expression.operand.table}.{expression.operand.name}"
    return f"{expression.function}({operand})"
