"""Shared support for deterministic SQL AST candidate expansion.

This module owns the schema-linking and AST utility contract used by independent
recursive, constraint, and extrema expanders. Capability modules must not import
private implementation details from one another.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any, Iterable, Sequence
import weakref

from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
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
from engine.numeric import parse_decimal
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import SchemaGraph, canon, is_surrogate_key


def ordering_requested(question: str) -> bool:
    """An order instruction, not a business noun.

    ``order`` and ``rank`` can be business nouns ("purchase order", "rank
    points"), while ``ordered`` can describe a measure ("quantity ordered").
    Accept only unambiguous verbs/adverbs, imperative or polite-command forms,
    and explicit participial ordering phrases.  Keep this parser deliberately
    lexical: it is only a candidate-expansion cue, not an interpretation layer.
    """
    text = " ".join(question.split())
    if re.search(
        r'\b(?:sort|sorted|alphabetically)\b'
        r'|\b(?:ascending|descending)\s+order\b'
        r'|\border\s+by\b',
        text, re.IGNORECASE,
    ):
        return True
    if re.search(
        r'(?:^|[.!?;]\s*)(?:please\s+)?(?:order|rank)\b'
        r'|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:order|rank)\b'
        r'|\b(?:please|kindly)\s+(?:order|rank)\b'
        r'|\b(?:and|then|to)\s+(?:order|rank)\b',
        text, re.IGNORECASE,
    ):
        return True

    # Participles describe ordering when followed by an ordering construction.
    # The measure phrases below are the important exception: "units ordered by
    # customer" says what was purchased, not how result rows should be sorted.
    measure_nouns = {"amount", "number", "quantity", "unit", "units", "volume"}
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    for index, word in enumerate(words):
        if word not in {"ordered", "ranked"}:
            continue
        suffix = words[index + 1:index + 4]
        if suffix[:2] == ["according", "to"] or suffix[:1] == ["alphabetically"]:
            return True
        if "by" in suffix:
            prior = index - 1
            while prior >= 0 and words[prior] in {
                "are", "be", "been", "being", "is", "was", "were",
            }:
                prior -= 1
            if word == "ordered" and prior >= 0 and words[prior] in measure_nouns:
                continue
            return True
    return False


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
        context = naming_context(self.schema)
        matches = []
        for schema_column in self.schema.by_table.get(table, ()):
            column = schema_column.ref
            compact = re.sub(r"[^a-z0-9]", "", column.name.lower())
            if is_surrogate_key(column.name) and not explicit_id and compact not in token_set:
                continue
            if not column_matches(column.name, token_set, table, context[table]):
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
            if is_surrogate_key(column.name) or not column.type.numeric:
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
            if numeric and (not column.type.numeric or is_surrogate_key(column.name)):
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
        set(columns), key=lambda column: (0 if is_surrogate_key(column.name) else 1, column.name)
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


def parse_number(token: str):
    if token in WORD_NUMBERS:
        return WORD_NUMBERS[token]
    if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return parse_decimal(token) if "." in token else int(token)
    return None


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


# Grammar words a question writes in lower case: "in", "and" or "are" is never Code2 'IN', Code 'AND' or
# Code 'ARE' (Spider world_1, 2026-10-02), and "enrolled in a Bachelors program" no section 'a', while a
# question that names such a value writes it in capitals ("the division AS", "a grade of A").
FUNCTION_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or",
    "than", "that", "the", "this", "to", "was", "were", "with",
})
# Words that name the kind of a column's values rather than what they are: "the emails" are email_address,
# "the role" role_code, "the cell phone" cell_number (canon() forms). "name" is not one: "the first student"
# names no first_name.
KIND_WORDS = frozenset({"code", "number", "address", "date", "description", "details", "value", "text",
                        "flag", "yn"})


_NAMING_CONTEXTS: "weakref.WeakKeyDictionary[Any, dict[str, frozenset[str]]]" = weakref.WeakKeyDictionary()


def naming_context(schema: Any) -> dict[str, frozenset[str]]:
    """For each table, the words that say what none of its columns holds alone: the words of the tables'
    names ("the treatment type description" names no treatment_type_code) and the words two of its columns'
    names share ("tourney" of tourney_name and tourney_date, Spider wta_1, 2026-10-02). Read once per
    schema."""
    context = _NAMING_CONTEXTS.get(schema)
    if context is None:
        entities = frozenset(word for table in schema.tables for word in name_tokens(table))
        context = {}
        for table in schema.tables:
            names = {column.ref.name for column in schema.by_table.get(table, ())}
            shared = Counter(word for name in names for word in set(name_tokens(name)))
            context[table] = entities | frozenset(word for word, count in shared.items() if count > 1)
        _NAMING_CONTEXTS[schema] = context
    return context


def naming_words(link_words: tuple[str, ...], context: frozenset[str]) -> tuple[str, ...]:
    """The words of a column's name a question must say to name the column: all but the words of its
    kind, when the others say what its values are ("email" names email_address): no function word ("of" of
    date_of_treatment) and none of ``context``, its table's naming_context."""
    content = tuple(word for word in link_words if word not in KIND_WORDS)
    if content and not set(content) & (FUNCTION_WORDS | context):
        return content
    return link_words


def column_matches(name: str, question_tokens: set[str], table: str | None = None,
                   context: frozenset[str] | None = None) -> bool:
    """Whether the question names the column ``name``: its words, or with its table's naming_context its
    words but those of its kind (naming_words)."""
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
        if context is not None:
            words = list(naming_words(tuple(words), context))
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


# Transactional measure vocabulary. A question can rank or total a summable
# fact-table measure without naming its column: "units sold" means the summed
# quantity column and "revenue"/"spend" mean the summed extended-amount column.
# Quantity nouns require an adjacent participle because the bare noun is a rate
# qualifier ("unit price") or a plain column mention ("quantity"); money nouns
# are inherently extended amounts. All sets hold canon() forms.
QUANTITY_MEASURE_NOUNS = frozenset({"unit", "qty", "quantity"})
MONEY_MEASURE_NOUNS = frozenset({
    "revenue", "spend", "spent", "spending", "sale", "turnover", "expenditure",
})
TRANSACTION_PARTICIPLES = frozenset({"sold", "purchased", "bought", "ordered", "shipped"})
QUANTITY_MEASURE_COLUMN_WORDS = frozenset({"quantity", "qty"})
MONEY_MEASURE_COLUMN_WORDS = frozenset({
    "total", "amount", "revenue", "sale", "spend", "value", "subtotal",
})
# A money noun that names the table ("sales" in a sales table) asks for that table's money total:
# "what's the sales in France", "total sales in Asia". It stays the entity when the question asks to
# count it ("how many sales", "number of sales") or to list it ("list the sales in France").
ROW_LISTING_COMMANDS = frozenset({"list", "show", "display"})


def money_total_position(question_tokens: Sequence[str], table_words: Iterable[str]) -> int | None:
    """Position of a money noun that names a table and asks for its money total, else None.

    Tokens and table words are canon() forms. Both planners apply this one rule, the own-data
    search (sql_search) and the world-join operand choice (encoder_overlay.read_op_all); each still
    requires a money measure column in that table, without which the noun keeps its entity reading.
    """
    question_tokens = tuple(question_tokens)
    if count_requested(question_tokens) or ROW_LISTING_COMMANDS & set(question_tokens):
        return None
    names = set(table_words)
    return next((index for index, token in enumerate(question_tokens)
                 if token in MONEY_MEASURE_NOUNS and token in names), None)


def money_total_columns(question: str, sch: Sequence[dict]) -> tuple[str, list[dict]] | None:
    """(table, its money-named numeric columns) when ``question`` asks for a money-named table's total.

    ``sch`` is the planner schema ({table, name, affinity, ...}). None when the rule does not fire
    or the table has no money column.
    """
    question_tokens = tokens(question)
    table_of = {word: entry["table"] for entry in sch for word in name_tokens(entry["table"])}
    position = money_total_position(question_tokens, table_of)
    if position is None:
        return None
    table = table_of[question_tokens[position]]
    columns = [entry for entry in sch
               if entry["table"] == table and entry.get("affinity") in ("INTEGER", "REAL")
               and not is_surrogate_key(entry["name"])
               and set(name_tokens(entry["name"])) & MONEY_MEASURE_COLUMN_WORDS]
    return (table, columns) if columns else None


def aggregates_money_column(query, table: str, names: Iterable[str]) -> bool:
    """Whether a typed query's answer aggregates one of ``table``'s money columns.

    A converted total (SUM(amount * rate)) counts: its operand references the column.
    """
    wanted = set(names)

    def references(expression) -> bool:
        if isinstance(expression, ColumnRef):
            return expression.name in wanted
        if isinstance(expression, BinaryExpr):
            return references(expression.left) or references(expression.right)
        return False

    if not isinstance(query, SelectQuery):
        return False
    return any(isinstance(item.expression, Aggregate) and item.expression.function in {"SUM", "AVG"}
               and references(item.expression.operand)
               and table in query.referenced_tables()
               for item in query.select)


@dataclass(frozen=True)
class ImplicitMeasure:
    """A question position that names a summable measure without an aggregate word."""

    position: int
    column_words: frozenset[str]
    participle: bool


def implicit_sum_measures(question_tokens: Sequence[str]) -> tuple[ImplicitMeasure, ...]:
    """Positions in canon()-normalized tokens that imply SUM over a measure column.

    "units sold" and "quantity purchased" assert summation over transactions even
    without "total"; bare money nouns such as "revenue" or "spend" do the same.
    Bare quantity nouns never fire: "unit price" is a rate and "quantity" alone is
    an ordinary column mention. Callers own schema-aware guards (a noun that names
    a real table or column is an entity/column mention, not an implicit measure).
    """
    out = []
    for index, token in enumerate(question_tokens):
        follower = question_tokens[index + 1] if index + 1 < len(question_tokens) else ""
        if token in QUANTITY_MEASURE_NOUNS:
            if follower in TRANSACTION_PARTICIPLES:
                out.append(ImplicitMeasure(index, QUANTITY_MEASURE_COLUMN_WORDS, True))
        elif token in MONEY_MEASURE_NOUNS:
            out.append(ImplicitMeasure(
                index, MONEY_MEASURE_COLUMN_WORDS, follower in TRANSACTION_PARTICIPLES,
            ))
    return tuple(out)


# Words that ask for a part as a fraction of its whole (canon() forms): "what share of the total amount
# comes from Paris", "the percentage of orders from Lyon".
SHARE_WORDS = frozenset({"share", "percentage", "percent", "proportion", "fraction"})


def _share_positions(question_tokens: Sequence[str], column_words: Iterable[str]) -> list[int]:
    """The positions of the share words that ask for a fraction: "what share of", "the percentage of
    orders", "as a percentage", a last word. A share word that names a column ("the maximum share of TV
    series") is that column; one before another word names a thing ("the highest share price", "the %
    discount"), and "share" as a verb ("customers who share a city") divides nothing (review,
    2026-10-02)."""
    column_words = set(column_words)
    tokens = tuple(question_tokens)
    return [index for index, token in enumerate(tokens)
            if token in SHARE_WORDS and token not in column_words
            and (tokens[index + 1:index + 2] in (("of",), ()) or tokens[max(0, index - 2):index] == ("as", "a"))]


def share_requested(question_tokens: Sequence[str], column_words: Iterable[str] = ()) -> bool:
    """Whether the question asks for a fraction of a whole (``_share_positions``)."""
    return bool(_share_positions(question_tokens, column_words))


# Words that end the noun phrase after "share of": "the share of the order amount comes from Paris".
_SHARE_PHRASE_ENDS = frozenset({
    "come", "came", "from", "in", "by", "for", "is", "are", "was", "were", "that", "which", "who", "with",
    "to", "do", "doe", "did", "of", "per", "each", "on", "at", "where", "and", "or", "account",
})


def share_cue(question_tokens: Sequence[str], schema: SchemaGraph) -> tuple[str, int] | None:
    """The aggregate a share asks for, at its word's position: a share is an aggregate of the rows it
    divides. "percentage of amount by city" and "the share of the order amount" sum the measure the
    phrase after "of" names in full; "share of orders by city" and "what percentage of customers are in
    Paris" count the rows (probe, 2026-10-02: the first two were listings that divided nothing)."""
    column_words = {word for column in schema.columns for word in name_tokens(column.ref.name)}
    tokens = tuple(question_tokens)
    position = next((index for index in _share_positions(tokens, column_words)
                     if tokens[index + 1:index + 2] == ("of",)), None)
    if position is None:
        return None
    phrase: set[str] = set()
    for token in tokens[position + 2:position + 8]:
        if token in _SHARE_PHRASE_ENDS:
            break
        phrase.add(token)
    named = any(words and words <= phrase
                for column in schema.columns
                if column.ref.type.numeric and not is_surrogate_key(column.ref.name)
                for words in (set(name_tokens(column.ref.name)),))
    return ("SUM" if named else "COUNT"), position


# A superlative of quantity ranks totals: "the most deposits" is the largest total of deposits. "The
# highest amount" stays out: it may name the largest single value as well as the largest total.
QUANTITY_SUPERLATIVES = frozenset({"most", "least", "fewest"})


def _numeric_measures(sch: Sequence[dict]) -> list[dict]:
    return [entry for entry in sch
            if entry.get("affinity") in ("INTEGER", "REAL") and not is_surrogate_key(entry["name"])]


def ranked_measure_columns(question: str, sch: Sequence[dict]) -> list[dict]:
    """The numeric columns a quantity superlative ranks totals of, else [].

    "which country has the most deposits" and "which bank has the fewest transfers" name the column
    right after the cue. "which country spent the most on catering" names no column; its money verb
    asks for the money columns. ``sch`` is the planner schema ({table, name, affinity, ...}).
    """
    question_tokens = tokens(question)
    cue = next((index for index, token in enumerate(question_tokens)
                if token in QUANTITY_SUPERLATIVES), None)
    if cue is None:
        return []
    numeric = _numeric_measures(sch)
    following = question_tokens[cue + 1:]
    named = [entry for entry in numeric
             if (words := name_tokens(entry["name"])) and following[:len(words)] == words]
    if named:
        return named
    money = {word for measure in implicit_sum_measures(question_tokens) for word in measure.column_words
             if question_tokens[measure.position] in MONEY_MEASURE_NOUNS}
    return [entry for entry in numeric if set(name_tokens(entry["name"])) & money]


def ranked_row_tables(question: str, sch: Sequence[dict]) -> list[str]:
    """The sheets whose rows a quantity superlative counts, else [].

    "which country has the most orders" counts the rows of the orders sheet, and "which country has the
    most banks" the rows of the sheet whose bank column names one bank per row. A measure column the
    superlative names is ranked_measure_columns' reading, not this one.
    """
    question_tokens = tokens(question)
    cue = next((index for index, token in enumerate(question_tokens)
                if token in QUANTITY_SUPERLATIVES), None)
    if cue is None or cue + 1 >= len(question_tokens):
        return []
    noun = question_tokens[cue + 1]
    return sorted({entry["table"] for entry in sch
                   if name_tokens(entry["table"]) == (noun,)
                   or (entry.get("affinity") == "TEXT" and name_tokens(entry["name"]) == (noun,))})


def counted_measure_columns(question: str, sch: Sequence[dict]) -> list[dict]:
    """The numeric columns that already hold the count a count cue asks for, else [].

    In a sheet whose ``transfers`` column holds each row's number of transfers, "how many transfers"
    asks for that column's total, not for the number of rows. The counted noun must be the column's
    whole name, right after "how many", "number of" or "count of".
    """
    question_tokens = tokens(question)
    starts = [index + 2 for index in range(len(question_tokens) - 1)
              if question_tokens[index:index + 2] in (("how", "many"), ("number", "of"), ("count", "of"))]
    return [entry for entry in _numeric_measures(sch)
            if (words := name_tokens(entry["name"]))
            and any(question_tokens[start:start + len(words)] == words for start in starts)]


def measure_words_after(question_tokens: Sequence[str], position: int) -> frozenset[str] | None:
    """Preferred measure-column words for an explicit aggregate cue at ``position``.

    "total spend" and "total quantity sold" carry the measure noun right after the
    aggregate word; without this the cue's numeric fallback is a semantically
    arbitrary tie. Returns None when no measure noun follows within two tokens.
    """
    for token in question_tokens[position + 1:position + 3]:
        if token in QUANTITY_MEASURE_NOUNS:
            return QUANTITY_MEASURE_COLUMN_WORDS
        if token in MONEY_MEASURE_NOUNS:
            return MONEY_MEASURE_COLUMN_WORDS
    return None


def value_set(values: Sequence[object]) -> set[object]:
    return {value for value in values if value is not None and str(value).strip()}


def value_count(values: Sequence[object]) -> int:
    return sum(value is not None and bool(str(value).strip()) for value in values)


def compatible_types(left: SQLType, right: SQLType) -> bool:
    return left == right or (left.numeric and right.numeric)


def name_tokens(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(canon(token) for token in re.findall(r"[A-Za-z0-9]+", spaced))


# A "%" standing alone is the word "percent" ("what % of the total amount comes from Paris" served the
# Paris total, 2026-10-02); after a number ("over 50%") it is the number's unit, and inside a word or
# quotes ("names like 'A%'") a pattern's wildcard.
_QUESTION_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?|(?<![\w%'\"])(?<!\d\s)%(?![\w'\"])")


def words(text: str) -> list[str]:
    """The lowercase words of a question, before any module's own normalization: the one word
    splitter the search, its expansions and the ranker read a question with."""
    return ["percent" if word == "%" else word for word in _QUESTION_WORD.findall(text.lower())]


def word_spans(text: str) -> list[tuple[int, int]]:
    """Where each of ``words(text)`` stands in ``text``."""
    return [match.span() for match in _QUESTION_WORD.finditer(text.lower())]


def tokens(text: str) -> tuple[str, ...]:
    out = []
    for token in words(text):
        if token.endswith("'s"):
            token = token[:-2]
        out.append(canon(token))
    return tuple(out)
