"""Shared support for deterministic SQL AST candidate expansion.

This module owns the schema-linking and AST utility contract used by independent
recursive, constraint, and extrema expanders. Capability modules must not import
private implementation details from one another.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence

from engine.sql_ast import (
    Aggregate,
    BooleanExpr,
    ColumnRef,
    Comparison,
    Join,
    Literal,
    Predicate,
    Query,
    SQLType,
    SelectItem,
    SelectQuery,
    Star,
    and_predicates,
    render_query,
)
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import SchemaGraph


WORD_NUMBERS = {
    "zero": 0, "one": 1, "single": 1, "two": 2, "couple": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10,
}
NOISE_VALUES = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "by", "for", "from", "in", "no", "not",
    "is", "of", "on", "or", "the", "to", "with", "without",
})


@dataclass(frozen=True)
class CountThreshold:
    predicates: tuple[tuple[str, int], ...]
    start: int
    end: int
    strength: float

    @property
    def values(self) -> frozenset[int]:
        return frozenset(value for _, value in self.predicates)


class ExpansionSupport:
    """Reusable schema-linking operations shared by independent expanders."""

    def __init__(self, schema: SchemaGraph, max_candidates: int = 180):
        self.schema = schema
        self.max_candidates = max(1, max_candidates)

    def entity_tables(
        self, question_tokens: tuple[str, ...], threshold: CountThreshold
    ) -> list[tuple[str, float]]:
        prefix = set(question_tokens[:max(threshold.start, 1)])
        all_tokens = set(question_tokens)
        scored = []
        for table in self.schema.tables:
            projection = self.projection_columns(question_tokens, table)
            table_words = set(semantic_tokens(table))
            prefix_overlap = len(table_words & prefix)
            global_overlap = len(table_words & all_tokens)
            mention_positions = [
                index for index, token in enumerate(question_tokens[:threshold.start])
                if token in table_words
            ]
            mention_score = 8.0 + 0.1 * (threshold.start - min(mention_positions)) \
                if mention_positions else 0.0
            score = 2.0 * len(projection) + 2.0 * prefix_overlap + global_overlap + mention_score
            scored.append((table, score))
        return sorted(scored, key=lambda item: (-item[1], item[0]))

    def counted_tables(
        self, question_tokens: tuple[str, ...], threshold: CountThreshold, entity_table: str
    ) -> list[tuple[str, float]]:
        suffix = set(question_tokens[
            max(0, threshold.start - 7):min(len(question_tokens), threshold.end + 7)
        ])
        scored = []
        for table in self.schema.tables:
            words = set(semantic_tokens(table))
            overlap = len(words & suffix)
            score = 3.0 * overlap
            if table != entity_table and overlap:
                score += 0.75
            scored.append((table, score))
        return sorted(scored, key=lambda item: (-item[1], item[0]))

    def having_structures(
        self,
        candidates: Sequence[ScoredQuery],
        entity_table: str,
        counted_table: str,
        threshold: CountThreshold,
    ) -> list[tuple[str, tuple[Join, ...], Predicate | None, float]]:
        structures = []
        required = {entity_table, counted_table}
        join_trees = self.schema.join_trees(required, preferred_root=counted_table, limit=4)
        for tree in join_trees:
            structures.append((tree.root, tree.joins, None, -0.2 * len(tree.joins)))
        if entity_table != counted_table and not join_trees:
            inferred = self.inferred_entity_join(entity_table, counted_table)
            if inferred is not None:
                structures.append((counted_table, (inferred,), None, -0.25))
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not isinstance(query.from_table, str):
                continue
            if not required <= physical_tables(query):
                continue
            where = clean_threshold_where(query.where, threshold.values)
            score = 0.5 + 0.2 * len(and_terms(where)) if where else 0.5
            structures.append((query.from_table, query.joins, where, score))
        dedup = {}
        for structure in structures:
            key = (structure[0], structure[1], repr(structure[2]))
            old = dedup.get(key)
            if old is None or structure[3] > old[3]:
                dedup[key] = structure
        return sorted(dedup.values(), key=lambda item: (-item[3], repr(item)))

    def inferred_entity_join(self, entity_table: str, counted_table: str) -> Join | None:
        """Infer a missing FK only from a unique parent key and near-subset child values."""
        entity_words = set(semantic_tokens(entity_table))
        options = []
        for parent in self.schema.by_table.get(entity_table, ()):
            parent_values = value_set(parent.values)
            if len(parent_values) < 2 or len(parent_values) < 0.95 * value_count(parent.values):
                continue
            for child in self.schema.by_table.get(counted_table, ()):
                if not compatible_types(parent.ref.type, child.ref.type):
                    continue
                child_values = value_set(child.values)
                if len(child_values) < 2 or value_count(child.values) < 1.2 * len(child_values):
                    continue
                overlap = len(parent_values & child_values) / len(child_values)
                if overlap < 0.9:
                    continue
                child_words = set(semantic_tokens(child.ref.name))
                parent_words = set(semantic_tokens(parent.ref.name))
                role_match = bool(child_words & entity_words)
                name_match = bool(child_words & parent_words)
                if not role_match and not name_match:
                    continue
                score = 4.0 * overlap + 2.0 * role_match + name_match
                options.append((score, parent.ref, child.ref))
        if not options:
            return None
        _, parent, child = max(
            options,
            key=lambda item: (item[0], item[1].name, item[2].name),
        )
        return Join(entity_table, child, parent)

    def projection_columns(
        self, question_tokens: tuple[str, ...], table: str
    ) -> list[tuple[ColumnRef, float, int]]:
        window = projection_window(question_tokens)
        token_set = {token for _, token in window}
        explicit_id = bool(token_set & {"id", "identifier", "code"})
        matches = []
        for schema_column in self.schema.by_table.get(table, ()):
            column = schema_column.ref
            compact = re.sub(r"[^a-z0-9]", "", column.name.lower())
            if is_id(column.name) and not explicit_id and compact not in token_set:
                continue
            if not column_matches(column.name, token_set, table):
                continue
            words = set(semantic_tokens(column.name))
            compact_hits = {index for index, token in window if token == compact}
            semantic_hits = {index for index, token in window if token in words}
            coverage = compact_hits or semantic_hits
            specificity = len(words & token_set)
            position = min(coverage) if coverage else len(question_tokens) + 5
            matches.append((column, float(specificity), position, coverage, words))
        out = []
        for column, specificity, position, coverage, words in matches:
            shadowed = any(
                words < other_words and coverage and coverage <= other_coverage
                for other, _, _, other_coverage, other_words in matches
                if other != column
            )
            if not shadowed:
                out.append((column, specificity, position))
        return sorted(out, key=lambda item: (item[2], -item[1], item[0].name))

    def projection_options(
        self, question: str, table: str, where: Predicate | None
    ) -> list[tuple[SelectItem, ...]]:
        question_tokens = tokens(question)
        filter_columns = {
            term.left for term in and_terms(where)
            if isinstance(term, Comparison) and isinstance(term.left, ColumnRef)
        } if where is not None else set()
        linked = [
            column for column, _, _ in self.projection_columns(question_tokens, table)
            if column not in filter_columns
        ]
        if not linked:
            linked = list(self.schema.display_columns(table)[:1])
        full = tuple(SelectItem(column) for column in dict.fromkeys(linked[:4]))
        options = [full]
        options.extend((SelectItem(column),) for column in linked[:4])
        return list(dict.fromkeys(options))

    def group_options(
        self,
        question: str,
        entity_table: str,
        counted_table: str,
        joins: tuple[Join, ...],
        projection: tuple[SelectItem, ...],
    ) -> list[tuple[ColumnRef, ...]]:
        projected = tuple(
            item.expression for item in projection if isinstance(item.expression, ColumnRef)
        )
        if projected:
            return [tuple(dict.fromkeys(projected))]
        if entity_table != counted_table:
            key = join_key(joins, entity_table)
            if key is not None:
                return [(key,)]
        question_tokens = tokens(question)
        all_tokens = set(question_tokens)
        columns = [
            schema_column.ref for schema_column in self.schema.by_table.get(entity_table, ())
            if column_matches(schema_column.ref.name, all_tokens, entity_table)
        ]
        ordered = columns
        options = [(column,) for column in dict.fromkeys(ordered)]
        return options or [tuple(self.schema.display_columns(entity_table)[:1])]

    @staticmethod
    def having_select_options(
        question: str, projection_options: list[tuple[SelectItem, ...]]
    ) -> list[tuple[tuple[SelectItem, ...], float]]:
        question_tokens = tokens(question)
        projection = projection_options[0]
        if top_level_count(question_tokens):
            return [((SelectItem(Aggregate("COUNT", Star())),), 3.0)]
        out = [(projection, 2.0)]
        if count_requested(question_tokens):
            count = SelectItem(Aggregate("COUNT", Star()))
            out.append(((count,) + projection, 4.0))
            out.append((projection + (count,), 3.5))
        out.extend((option, 0.25) for option in projection_options[1:3])
        return out

    def numeric_comparisons(
        self,
        question_tokens: tuple[str, ...],
        exclude_positions: frozenset[int] = frozenset(),
    ) -> list[Comparison]:
        out = []
        mentions = self.mentioned_columns(question_tokens, numeric=True)
        for index, token in enumerate(question_tokens):
            if index in exclude_positions:
                continue
            value = parse_number(token)
            if value is None:
                continue
            if isinstance(value, int) and 1900 <= value <= 2100:
                year_columns = [
                    column.ref for column in self.schema.columns
                    if "year" in semantic_tokens(column.ref.name) or column.ref.type == SQLType.DATE
                ]
                targets = year_columns[:3]
            else:
                nearby = sorted(
                    mentions,
                    key=lambda item: (abs(item[0] - index), item[1].table, item[1].name),
                )
                targets = [column for position, column in nearby if abs(position - index) <= 4][:3]
            operator = nearby_operator(question_tokens, index)
            for target in dict.fromkeys(targets):
                if target.type == SQLType.DATE and isinstance(value, int):
                    if operator == ">":
                        out.append(Comparison(
                            target, ">=", Literal(f"{value + 1:04d}-01-01", SQLType.DATE)
                        ))
                    elif operator in {">=", "<"}:
                        out.append(Comparison(
                            target, operator, Literal(f"{value:04d}-01-01", SQLType.DATE)
                        ))
                    elif operator == "<=":
                        out.append(Comparison(
                            target, "<", Literal(f"{value + 1:04d}-01-01", SQLType.DATE)
                        ))
                    continue
                out.append(Comparison(target, operator, Literal(value, target.type)))
        return out

    def threshold_targets_column(
        self, question_tokens: tuple[str, ...], threshold: CountThreshold
    ) -> bool:
        number_positions = [
            index for index in range(
                threshold.start, min(len(question_tokens), threshold.end + 1)
            )
            if parse_number(question_tokens[index]) is not None
        ]
        if not number_positions:
            return False
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if is_id(column.name) or not column.type.numeric:
                continue
            for number_position in number_positions:
                local = set(question_tokens[
                    max(0, number_position - 3):min(len(question_tokens), number_position + 2)
                ])
                if column_matches(column.name, local):
                    return True
        return False

    def mentioned_columns(
        self, question_tokens: tuple[str, ...], numeric: bool
    ) -> list[tuple[int, ColumnRef]]:
        out = []
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if numeric and (not column.type.numeric or is_id(column.name)):
                continue
            words = set(semantic_tokens(column.name))
            for index, token in enumerate(question_tokens):
                if token in words:
                    out.append((index, column))
        return out


def build_candidate(
    query: Query, score: float, evidence: tuple[str, ...]
) -> ScoredQuery | None:
    try:
        sql = render_query(query)
    except (TypeError, ValueError):
        return None
    return ScoredQuery(query, sql, score, evidence)


def clean_threshold_where(
    predicate: Predicate | None, values: frozenset[int]
) -> Predicate | None:
    if predicate is None:
        return None
    kept = []
    for term in and_terms(predicate):
        if linker_noise(term):
            continue
        if isinstance(term, Comparison) and isinstance(term.right, Literal):
            try:
                if int(term.right.value) in values:
                    continue
            except (TypeError, ValueError):
                pass
        kept.append(term)
    return and_predicates(kept)


def and_terms(predicate: Predicate | None) -> tuple[Predicate, ...]:
    if predicate is None:
        return ()
    if isinstance(predicate, BooleanExpr) and predicate.operator == "AND":
        return tuple(term for child in predicate.terms for term in and_terms(child))
    return (predicate,)


def linker_noise(predicate: Predicate) -> bool:
    return (
        isinstance(predicate, Comparison)
        and isinstance(predicate.right, Literal)
        and str(predicate.right.value).strip().lower() in NOISE_VALUES
    )


def physical_tables(query: SelectQuery) -> set[str]:
    out = {query.from_table} if isinstance(query.from_table, str) else set()
    out.update(join.table for join in query.joins)
    return out


def join_key(joins: tuple[Join, ...], table: str) -> ColumnRef | None:
    columns = [
        column for join in joins if len(join.predicates) == 1
        for column in join.predicates[0]
        if column.table == table
    ]
    return sorted(
        set(columns), key=lambda column: (0 if is_id(column.name) else 1, column.name)
    )[0] if columns else None


def entity_join_key(query: SelectQuery, table: str) -> ColumnRef | None:
    return join_key(query.joins, table)


def expression_table(expression) -> str | None:
    if isinstance(expression, ColumnRef):
        return expression.table
    if isinstance(expression, Aggregate) and isinstance(expression.operand, ColumnRef):
        return expression.operand.table
    return None


def unique_predicates(predicates: Iterable[Predicate]) -> list[Predicate]:
    out = []
    seen = set()
    for predicate in predicates:
        key = repr(predicate)
        if key not in seen:
            seen.add(key)
            out.append(predicate)
    return out


def nearby_operator(question_tokens: tuple[str, ...], index: int) -> str:
    before = question_tokens[max(0, index - 4):index]
    after = question_tokens[index + 1:index + 3]
    if "not" in before and len(before) >= 2 and before[-2:] == ("more", "than"):
        return "<="
    if "before" in before or "under" in before or "below" in before:
        return "<"
    if "after" in before or "over" in before or "above" in before:
        return ">"
    if "since" in before:
        return ">="
    if len(before) >= 2 and before[-2:] in {
        ("longer", "than"), ("more", "than"), ("greater", "than")
    }:
        return ">"
    if len(before) >= 2 and before[-2:] in {
        ("shorter", "than"), ("less", "than"), ("fewer", "than")
    }:
        return "<"
    if after == ("or", "more") or after in {("or", "after"), ("or", "later")}:
        return ">="
    if after in {("or", "before"), ("or", "earlier")}:
        return "<="
    return "="


def column_requested_as_output(column: ColumnRef, question: str) -> bool:
    normalized = " ".join(tokens(question))
    words = semantic_tokens(column.name)
    temporal = bool(set(words) & {"year", "date", "time"})
    if temporal and "production time" in normalized:
        return True
    return temporal and bool(
        re.search(r"\b(?:what|which|show|list|give|find).{0,80}\b(?:year|date|time)\b", normalized)
    )


def top_level_count(question_tokens: tuple[str, ...]) -> bool:
    normalized = " ".join(question_tokens[:8])
    return normalized.startswith("how many") or normalized.startswith("what is the number") \
        or normalized.startswith("what are the number")


def count_requested(question_tokens: tuple[str, ...]) -> bool:
    return bool(set(question_tokens) & {"count", "number"}) \
        or "how many" in " ".join(question_tokens)


def explicit_order(question_tokens: tuple[str, ...]) -> bool:
    return bool(set(question_tokens) & {"order", "ordered", "sort", "sorted", "top", "bottom"})


def parse_number(token: str) -> int | float | None:
    if token in WORD_NUMBERS:
        return WORD_NUMBERS[token]
    if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return float(token) if "." in token else int(token)
    return None


def is_id(name: str) -> bool:
    words = name_tokens(name)
    return bool(words) and words[-1] in {"id", "identifier", "key", "code"}


def projection_window(question_tokens: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
    commands = {"find", "give", "list", "return", "show", "what", "which"}
    boundaries = {
        "has", "have", "having", "shared", "that", "under", "where", "which",
        "who", "whose", "with",
    }
    late_commands = [
        index for index, token in enumerate(question_tokens) if token in commands and index > 2
    ]
    if late_commands:
        start = late_commands[-1] + 1
        end = next(
            (index for index in range(start + 1, len(question_tokens))
             if question_tokens[index] in boundaries),
            len(question_tokens),
        )
        return tuple(enumerate(question_tokens[start:end], start))
    start = 1 if question_tokens and question_tokens[0] in commands else 0
    end = next(
        (index for index in range(max(start + 1, 2), len(question_tokens))
         if question_tokens[index] in boundaries),
        len(question_tokens),
    )
    return tuple(enumerate(question_tokens[start:end], start))


def column_matches(name: str, question_tokens: set[str], table: str | None = None) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if compact in question_tokens:
        return True
    special = {
        "fname": ({"first"}, {"name"}), "firstname": ({"first"}, {"name"}),
        "lname": ({"last"}, {"name"}), "lastname": ({"last"}, {"name"}),
        "sex": ({"sex", "gender"},), "gender": ({"sex", "gender"},),
        "mpg": ({"mpg", "mile"}, {"mpg", "gallon"}),
    }
    groups = special.get(compact)
    if groups is None:
        aliases = {
            "maker": {"maker", "manufacturer"},
            "weight": {"weight", "weigh", "weighed", "weighing"},
            "accelerate": {"accelerate", "acceleration"},
            "hometown": {"hometown", "town", "city"},
            "death": {"death", "killed"},
        }
        table_words = set(name_tokens(table)) if table is not None else set()
        words = [
            word for word in name_tokens(name)
            if word != "of" and (word not in table_words or len(name_tokens(name)) == 1)
        ]
        groups = tuple(aliases.get(word, {word}) for word in words)
    return bool(groups) and all(bool(group & question_tokens) for group in groups)


def semantic_tokens(name: str) -> tuple[str, ...]:
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    special = {
        "fname": ("first", "name"), "firstname": ("first", "name"),
        "lname": ("last", "name"), "lastname": ("last", "name"),
        "sex": ("sex", "gender"), "gender": ("sex", "gender"),
    }
    words = list(special.get(compact, name_tokens(name)))
    synonyms = {
        "maker": ("manufacturer",), "manufacturer": ("maker",),
        "weight": ("weigh", "weighed", "weighing"),
        "accelerate": ("acceleration",), "hometown": ("town", "city"),
        "death": ("killed",), "song": ("songs",), "visit": ("visited",),
    }
    expanded = list(words)
    for word in words:
        expanded.extend(synonyms.get(word, ()))
    return tuple(dict.fromkeys(expanded))


def value_set(values: Sequence[object]) -> set[object]:
    return {value for value in values if value is not None and str(value).strip()}


def value_count(values: Sequence[object]) -> int:
    return sum(value is not None and bool(str(value).strip()) for value in values)


def compatible_types(left: SQLType, right: SQLType) -> bool:
    return left == right or (left.numeric and right.numeric)


def name_tokens(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(canon(token) for token in re.findall(r"[A-Za-z0-9]+", spaced))


def tokens(text: str) -> tuple[str, ...]:
    out = []
    for token in re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", text.lower()):
        if token.endswith("'s"):
            token = token[:-2]
        out.append(canon(token))
    return tuple(out)


def canon(word: str) -> str:
    word = word.lower().strip()
    if word == "ids":
        return "id"
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
