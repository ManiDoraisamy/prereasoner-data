"""Shared support for deterministic SQL AST candidate expansion.

This module owns the schema-linking and AST utility contract used by independent
recursive, constraint, and extrema expanders. Capability modules must not import
private implementation details from one another.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
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
    SetQuery,
    Star,
    and_predicates,
    render_query,
)
from engine.closed_class import COMPARATIVES, QUANTITY_COMPARATIVES, spacy_model
from engine.numeric import NUMBER_TEXT, NUMBER_WORD, parse_decimal
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import SchemaGraph, canon, is_surrogate_key, name_words


# The stored codes of a flag's two values, and the words a flag's name opens or ends with that say it is one
# ("IsOfficial", "has_parking", "abandoned_yn", "chargeable_flag").
_FLAG_TRUE = ("t", "true", "y", "yes", "1")
_FLAG_FALSE = frozenset({"f", "false", "n", "no", "0"})
_FLAG_LEADS = frozenset({"is", "has"})
_FLAG_ENDS = frozenset({"yn", "flag", "ind"})
_FLAG_NEGATIONS = frozenset({"not", "no", "non", "never", "nt"})
_FLAG_FIELDS: "weakref.WeakKeyDictionary[Any, tuple[tuple[Any, Any, tuple[str, ...]], ...]]" = weakref.WeakKeyDictionary()


def flag_fields(schema: Any) -> tuple[tuple[Any, Any, tuple[str, ...]], ...]:
    """The fields of ``schema`` that hold a yes/no flag, each with its stored true value and the words that name it:
    world_1's countrylanguage.IsOfficial ('T'/'F') is named "official", dog_kennels' abandoned_yn ('1'/'0')
    "abandoned". A field's stored values must all be one flag code set with its true value among them, and its name
    must say it is a flag (an affix): a Newsletter field holding Yes and No is a value the question states ("responses
    with Newsletter No"), and a count holding 0 and 1 is no flag. Read once per schema."""
    known = _FLAG_FIELDS.get(schema)
    if known is not None:
        return known
    found = []
    for schema_column in getattr(schema, "columns", ()):
        stored = {str(value).strip().lower(): value for value in schema_column.values
                  if value is not None and str(value).strip()}
        truthy = next((code for code in _FLAG_TRUE if code in stored), None)
        if truthy is None or not set(stored) <= set(_FLAG_TRUE) | _FLAG_FALSE:
            continue
        words = [canon(word) for word in name_words(schema_column.ref.name)]
        affixed = bool(words) and (words[0] in _FLAG_LEADS or words[-1] in _FLAG_ENDS)
        if words and words[0] in _FLAG_LEADS:
            words = words[1:]
        if words and words[-1] in _FLAG_ENDS:
            words = words[:-1]
        if not words or not affixed:
            continue
        found.append((schema_column.ref, stored[truthy], tuple(words)))
    result = tuple(found)
    try:
        _FLAG_FIELDS[schema] = result
    except TypeError:
        pass
    return result


def flag_comparisons(said: Sequence[str], schema: Any) -> tuple[tuple[int, int, Any], ...]:
    """The flags ``said`` (tokens) names as true, each as (start, end, the comparison ``field = true value``): "How many
    official languages does Afghanistan have?" counts only countrylanguage rows with IsOfficial = 'T' (Spider DEV 738,
    748, 768, 770, 786, 2026-10-17: 14 world_1 questions saying "official" were refused or wrong without it). A
    negation just before the word ("English is not the official language") leaves the flag to the exclusion that
    reads it."""
    found = []
    for column, value, words in flag_fields(schema):
        size = len(words)
        for start in range(len(said) - size + 1):
            if tuple(said[start:start + size]) != words:
                continue
            if _FLAG_NEGATIONS & set(said[max(0, start - 3):start]):
                continue
            # A code said after the word states the flag's value itself: "official 'F'", "abandoned yes".
            if said[start + size:start + size + 1] and said[start + size] in set(_FLAG_TRUE) | _FLAG_FALSE:
                continue
            found.append((start, start + size, Comparison(column, "=", Literal(value, column.type))))
    return tuple(found)


@dataclass(frozen=True)
class PopulationPresenceReading:
    """One known attribute two populations the question names both (or either) hold: "Which states have both owners
    and professionals living there?" asks for the states of owners INTERSECT those of professionals (the Codex review's
    population-presence reading, ported 2026-10-17; Spider DEV 918, 919)."""
    operator: str
    left: ColumnRef
    right: ColumnRef


_POPULATION_PRESENCE: "weakref.WeakKeyDictionary[Any, dict[str, PopulationPresenceReading | None]]" = (
    weakref.WeakKeyDictionary())
_PRESENCE_WORDS = frozenset({"present", "presence", "exist", "exists", "occur", "occurs", "found", "live", "living",
                             "reside", "residing", "recorded", "listed", "appear"})


def population_presence_reading(question: str, schema: Any) -> PopulationPresenceReading | None:
    """The values two populations both or either hold, where the question says only that: one "both" or "either", two
    tables named right after it and joined by "and" or "or", one field of the same name in both said before it, and no
    other word but grammar and words of presence ("living there"). Values, numbers, another entity or an ambiguous
    field leave the request unread; no relationship between the two populations is invented or used."""
    memo = _POPULATION_PRESENCE.setdefault(schema, {}) if isinstance(schema, SchemaGraph) else {}
    if question in memo:
        return memo[question]
    said = tokens(question)
    cues = [index for index, token in enumerate(said) if token in {"both", "either"}]
    reading = None
    if len(cues) == 1:
        cue = cues[0]

        def tables_at(position):
            if said[position:position + 1] == ("the",):
                position += 1
            return [(table, position + len(label)) for table in schema.tables
                    if (label := name_tokens(table)) and said[position:position + len(label)] == label]

        lefts = tables_at(cue + 1)
        if len(lefts) == 1:
            left, middle = lefts[0]
            connector = "and" if said[cue] == "both" else "or"
            rights = tables_at(middle + 1) if said[middle:middle + 1] == (connector,) else []
            if len(rights) == 1:
                right, end = rights[0]
                pairs = [(a.ref, b.ref) for a in schema.by_table.get(left, ()) for b in schema.by_table.get(right, ())
                         if left != right and not is_surrogate_key(a.ref.name)
                         and name_tokens(a.ref.name) == name_tokens(b.ref.name)
                         and set(name_tokens(a.ref.name)) <= set(said[:cue])
                         and a.ref.type == b.ref.type and a.ref.type != SQLType.UNKNOWN]
                if len(pairs) == 1:
                    first, second = pairs[0]
                    grammar = FUNCTION_WORDS | _PRESENCE_WORDS | {
                        "which", "what", "where", "find", "list", "show", "give", "all", "there", "have", "has"}
                    if set(said[:cue]) <= grammar | set(name_tokens(first.name)) and set(said[end:]) <= grammar:
                        reading = PopulationPresenceReading("INTERSECT" if said[cue] == "both" else "UNION",
                                                            first, second)
    if isinstance(schema, SchemaGraph):
        if len(memo) >= 128:
            del memo[next(iter(memo))]
        memo[question] = reading
    return reading


def population_presence_witness(query: Query, reading: PopulationPresenceReading) -> bool:
    """Whether ``query`` is that set: each population's own known values of the field, nothing joined or filtered."""
    if not isinstance(query, SetQuery) or query.operator != reading.operator:
        return False
    for branch, field in ((query.left, reading.left), (query.right, reading.right)):
        if (not isinstance(branch, SelectQuery) or branch.from_table != field.table or branch.from_alias
                or branch.joins or branch.group_by or branch.having is not None or branch.order_by
                or branch.limit is not None or tuple(item.expression for item in branch.select) != (field,)
                or branch.where != Comparison(field, "IS NOT", Literal(None))):
            return False
    return True


def complete_projection_requested(question: str) -> bool:
    """An explicit request to display complete records, including their fields: "show all records", and "all the
    information about hiring", "display all the details of employees", "return all columns of the albums" (Spider DEV
    291, held-out Spider train 147, 297, 728, 2026-10-12: no reading showed every field; each gold is SELECT *)."""
    return bool(re.search(r'\b(?:show|list|display)\s+(?:all|every)\s+(?:rows?|records?|columns?|fields?)\b',
                          question, re.I) or
                re.search(r'\b(?:all|every)\s+(?:the\s+)?(?:information|info|details?|data|columns?|fields?|attributes?)'
                          r'\b', question, re.I))


# "in alphabetical order", "alphabetically": a text field ordered A to Z; with a reversing word, Z to A.
ALPHABETICAL_WORDS = frozenset({"alphabetic", "alphabetical", "alphabetically"})
REVERSE_ORDER_WORDS = frozenset({"reverse", "reversed", "descending", "desc"})


def ordering_requested(question: str) -> bool:
    """An order instruction, not a business noun.

    ``order`` and ``rank`` can be business nouns ("purchase order", "rank
    points"), while ``ordered`` can describe a measure ("quantity ordered").
    Accept only unambiguous verbs/adverbs, imperative or polite-command forms,
    and explicit participial ordering phrases.  Keep this parser deliberately
    lexical: it is only a candidate-expansion cue, not an interpretation layer.
    """
    text = " ".join(question.split())
    # "in the order of birth date" orders as "ordered by birth date" does (Spider DEV 455, 2026-10-12: nothing was
    # ordered, and the birth date went unused).
    if re.search(
        r'\b(?:sort|sorted|alphabetical|alphabetically)\b'
        r'|\b(?:ascending|descending)\s+order\b'
        r'|\border\s+by\b'
        r'|\bin\s+(?:the\s+)?order\s+of\b',
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


@dataclass(frozen=True)
class AggregateThreshold:
    """A threshold stated on an aggregate something has: "customers with a total amount over 10" is SUM(amount) > 10,
    its aggregate word at ``position``."""
    function: str
    column: ColumnRef
    operator: str
    value: Any
    position: int


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
            # A workbook of one table counts its own rows whatever the question calls them: "which country has
            # the most leads" over a sheet named responses was refused (an owner's question, 2026-10-01), while
            # "how many leads per country" was answered.
            if len(self.schema.tables) == 1:
                score = max(score, 1.0)
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
        # A candidate's joins beyond the fewest that meet the tables carry its filters, or else only repeat the rows
        # counted: "countries with at least 3 cities" counted each city's addresses (held-out Spider train 296,
        # 2026-10-11), and the join tree of the two tables fell out of the pool below them.
        fewest = min((len(tree.joins) for tree in join_trees), default=None)
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not isinstance(query.from_table, str):
                continue
            if not required <= physical_tables(query):
                continue
            where = clean_threshold_where(query.where, threshold.values)
            score = 0.5 + 0.2 * len(and_terms(where)) if where else 0.5
            if where is None and fewest is not None:
                score -= 0.8 * max(0, len(query.joins) - fewest)
            structures.append((query.from_table, query.joins, where, score))
        dedup = {}
        for structure in structures:
            key = (structure[0], structure[1], repr(structure[2]))
            old = dedup.get(key)
            if old is None or structure[3] > old[3]:
                dedup[key] = structure
        return sorted(dedup.values(), key=lambda item: (-item[3], repr(item)))

    def inferred_entity_join(self, entity_table: str, counted_table: str) -> Join | None:
        """Construct a join from the canonical discovery owner's exact-key proof."""
        options = [key for key in self.schema.discovered_foreign_keys
                   if key.from_column.table == counted_table and key.to_column.table == entity_table]
        if not options:
            return None
        key = min(options, key=lambda key: (-key.confidence, key.signature))
        return Join(entity_table, key.from_column, key.to_column, additional=key.additional_columns)

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
        # A table named as one of the items to show is shown by its display field: "customer and product category for
        # each purchase", a decomposition leaf, asks for each purchase's customer (Chrome gate 2026-10-17: the leaf was
        # refused for showing only the category, and complex-category-gaps could not be split). Not a table named as
        # the rows' owner ("for each purchase", "of customers") nor one modifying a field ("product category"). An item
        # is one "and" or "or" joins to another: "the most common amenity in the dorms" shows no dorm (held-out Spider
        # train 882, 2026-10-17).
        if not matches:
            name = tuple(canon(word) for word in name_words(table))
            positions = [index for index, _ in window]
            displays = self.schema.display_columns(table)
            for index in positions:
                after = index + len(name)
                if (name and displays and tuple(question_tokens[index:after]) == name
                        and question_tokens[index - 1:index] not in (("each",), ("every",), ("per",), ("of",))
                        and (question_tokens[after:after + 1] in (("and",), ("or",))
                             or question_tokens[index - 1:index] in (("and",), ("or",)))):
                    matches.append((displays[0], 1.0, index, {index}, set(semantic_tokens(displays[0].name))))
                    break
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
        # An explicit list instruction's fields, when this table realizes every one, are shown first and in its order:
        # "Which makers designed more than 3 car models? List full name and the id." (Spider DEV 149, 2026-10-14).
        requested = self.requested_fields(question, table)
        if requested:
            options.insert(0, tuple(SelectItem(column) for column in requested))
        return list(dict.fromkeys(options))

    def requested_fields(self, question: str, table: str) -> tuple[ColumnRef, ...]:
        """The fields of ``table`` an explicit list instruction asks for (request_fields), in its order; empty unless
        the table realizes every field it asks."""
        fields = []
        for refs in request_fields(question, self.schema):
            realized = [ref for ref in refs if ref.table == table]
            if not realized:
                return ()
            fields.append(realized[0])
        return shown_once(fields, question, self.schema)

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
            if entity_table != counted_table:
                return [entity_groups(self.schema, entity_table, joins, tuple(dict.fromkeys(projected)))]
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

    def having_select_options(
        self, question: str, projection_options: list[tuple[SelectItem, ...]]
    ) -> list[tuple[tuple[SelectItem, ...], float]]:
        question_tokens = tokens(question)
        projection = projection_options[0]
        if top_level_count(question_tokens):
            return [((SelectItem(Aggregate("COUNT", Star())),), 3.0)]
        out = [(projection, 2.0)]
        if count_requested(question, (column.ref for column in self.schema.columns)):
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
        previous = None     # (index, targets) of the last number compared
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
                # A year compares the year field the question names beside it, by a word not every year field's name
                # holds, where it names one: "orchestras founded after 2008" is the orchestra's Year_of_Founded, not
                # the conductor's Year_of_Work, the first of the schema's year fields (Spider DEV 846, 2026-10-17).
                shared = set.intersection(*(set(name_tokens(column.name)) for column in year_columns)) if (
                    year_columns) else set()
                named = [column for position, column in sorted(
                    mentions, key=lambda item: (abs(item[0] - index), item[1].table, item[1].name))
                         if abs(position - index) <= 4 and column in year_columns
                         and question_tokens[position] not in shared]
                targets = list(dict.fromkeys(named))[:3] or year_columns[:3]
            else:
                nearby = sorted(
                    mentions,
                    key=lambda item: (abs(item[0] - index), item[1].table, item[1].name),
                )
                targets = [column for position, column in nearby if abs(position - index) <= 4][:3]
            operator = nearby_operator(question_tokens, index)
            # A comparison that continues the one before ("age above 35 and below 50") compares the same field, however
            # far its name is: "below 50" once compared nothing, so a set difference of the two was complete. One whose
            # number a noun follows compares that noun: "a grade of over 5 and 2 or more friends" (Spider DEV 911).
            following = question_tokens[index + 1:index + 4]
            if following[:2] in {("or", "more"), ("or", "fewer"), ("or", "less"), ("or", "above"), ("or", "below"),
                                 ("or", "greater"), ("or", "higher"), ("or", "lower")}:
                following = following[2:]
            following = following[:1]
            if (not targets and operator != "=" and previous is not None and index - previous[0] <= 4
                    and set(question_tokens[previous[0] + 1:index]) & {"and", "or", "but"}
                    and (not following or following[0] in FUNCTION_WORDS or not following[0].isalpha())):
                targets = previous[1]
            if targets:
                previous = (index, targets)
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

    def aggregate_thresholds(self, question_tokens: tuple[str, ...]) -> list[AggregateThreshold]:
        """Each threshold the question states on an aggregate something has rather than on each row: "customers with a
        total amount over 10" compares each customer's SUM of amount, "students whose average grade is above 80" an
        AVG. The holding word, the aggregate word, the field's name, a copula and the comparison come in that order
        with nothing else between them: "the total amount of orders over 10" totals the orders over 10, and "total
        Amount greater than 100" the amounts greater than 100."""
        out = []
        for index, token in enumerate(question_tokens):
            value = parse_number(token)
            operator = nearby_operator(question_tokens, index) if value is not None else "="
            if operator == "=":
                continue
            end = index
            while end > 0 and question_tokens[end - 1] in THRESHOLD_WORDS:
                end -= 1
            while end > 0 and question_tokens[end - 1] in COPULAS:
                end -= 1
            for schema_column in self.schema.columns:
                phrase = name_tokens(schema_column.ref.name)
                start = end - len(phrase)
                function = AGGREGATE_CUES.get(question_tokens[start - 1]) if phrase and start > 0 else None
                if function is None or question_tokens[start:end] != phrase:
                    continue
                holder = start - 2
                while holder >= 0 and question_tokens[holder] in {"a", "an", "the", "their", "its"}:
                    holder -= 1
                if holder >= 0 and question_tokens[holder] in _HOLDING_WORDS:
                    out.append(AggregateThreshold(function, schema_column.ref, operator, value, start - 1))
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
            if numeric and not column.type.numeric:
                continue
            if numeric and is_surrogate_key(column.name):
                # A key is compared only where its name introduces the number: "excluding Person_ID 2" (review F5,
                # 2026-10-11, where the 2 was compared with the age named before it).
                phrase = name_tokens(column.name)
                out.extend((start + len(phrase) - 1, column) for start in range(len(question_tokens) - len(phrase))
                           if question_tokens[start:start + len(phrase)] == phrase
                           and parse_number(question_tokens[start + len(phrase)]) is not None)
                continue
            words = set(semantic_tokens(column.name))
            for index, token in enumerate(question_tokens):
                # A field is named in another form of its word too: "teachers aged either 32 or 33" compares Age
                # (Spider DEV 389, 390, 2026-10-17: no reading compared it).
                if token in words or (numeric and any(inflects(token, word) or says_in_another_form(token, word)
                                                      for word in words)):
                    out.append((index, column))
        return out


def inflects(word: str, name_word: str) -> bool:
    """Whether ``word`` is an inflection of ``name_word`` too short for a shared stem: "aged" of Age, "priced" of
    price, "weighing" of weigh."""
    if len(name_word) < 3 or not word.isalpha():
        return False
    forms = {name_word + ending for ending in ("s", "es", "d", "ed", "ing")}
    if name_word.endswith("e"):
        forms.add(name_word[:-1] + "ing")
    return word in forms


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


def entity_groups(schema, entity_table: str, joins: tuple[Join, ...],
                  columns: tuple[ColumnRef, ...]) -> tuple[ColumnRef, ...]:
    """The groups that ``columns`` of ``entity_table`` make of its rows joined to others: the columns, and the
    entity's join key when their values do not tell its rows apart and the key does. Grouped by name alone, the two
    high schoolers named Jordan counted their friends together (Spider DEV 885, 888, 2026-10-10); a key that repeats
    is another table's ("countrylanguage"."CountryCode"), and the language is the group."""
    key = (join_key(joins, entity_table)
           if all(column.table == entity_table for column in columns) and schema.repeats(columns) else None)
    if key is not None and schema.repeats((key,)):
        key = None
    return tuple(dict.fromkeys((*columns, key) if key is not None else columns))


# Words after which "by" sorts ("ordered by age"); the noun "orders by city" groups.
_ORDERING_WORDS = frozenset({"order", "ordered", "sort", "sorted", "rank", "ranked"})
# Words after which "by" names groups although a participle ("grouped by", "broken down by").
_GROUPING_WORDS = frozenset({"grouped", "down", "split", "categorized", "classified", "segmented", "bucketed"})
# Participles that do not end in "-ed" ("written by Joseph Kuhr").
_IRREGULAR_PARTICIPLES = frozenset({
    "written", "done", "made", "held", "won", "given", "taken", "driven", "shown", "seen", "known", "built",
    "sold", "bought", "taught", "paid", "led", "run", "kept", "lost", "drawn", "chosen", "spoken", "brought",
    "caught", "found", "hit", "set", "put", "sung", "drunk", "eaten", "flown", "worn", "begun",
})


def by_groups(question_tokens: tuple[str, ...], raw_words: Sequence[str], index: int) -> bool:
    """Whether the "by" at ``index`` asks for groups ("total sales by city", "orders by city") rather than
    an order ("ordered by age") or who acted ("pets owned by students", "cartoons written by Joseph Kuhr":
    Spider DEV, 2026-10-02, both counted one group per student or writer)."""
    if index == 0 or question_tokens[index] != "by":
        return False
    before = question_tokens[index - 1]
    if before in _GROUPING_WORDS:
        return True
    if before in _ORDERING_WORDS:
        return raw_words[index - 1] == "orders"
    return not (before in _IRREGULAR_PARTICIPLES or (len(before) > 4 and before.endswith("ed")))


def positive_predicate(predicate: Predicate | None) -> Predicate | None:
    """The rows a denial excludes: ``predicate`` with each "!=" read as "=" ("students who do not have a cat"
    exclude the students who have one)."""
    if predicate is None:
        return None
    if isinstance(predicate, BooleanExpr):
        return BooleanExpr(predicate.operator, tuple(positive_predicate(term) for term in predicate.terms))
    if isinstance(predicate, Comparison) and predicate.operator in {"!=", "<>"}:
        return Comparison(predicate.left, "=", predicate.right)
    return predicate


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


# The words that exclude the row holding the number after them: "excluding Person_ID 2".
EXCLUDING_WORDS = frozenset({"excluding", "except"})
# The single words that set the comparison of the number after them, and the operator each sets.
_COMPARISON_CUES = {"before": "<", "under": "<", "below": "<", "after": ">", "over": ">", "above": ">", "since": ">="}
# A negated comparison's operator: "not higher than 4" is at most 4, and rows not over 4 are at most 4.
COMPLEMENTS = {">": "<=", "<": ">=", ">=": "<", "<=": ">"}


def comparative_operator(question_tokens: tuple[str, ...], index: int) -> str | None:
    """The operator the comparative at ``index`` makes ("more", "fewer" or one of ``COMPARATIVES``), its complement
    when "not" or "no" negates it: "no more than 4000" is at most 4000, "a membership level not higher than 4" at most
    4. None for any other word."""
    operator = {**QUANTITY_COMPARATIVES, **COMPARATIVES}.get(question_tokens[index])
    if operator is None:
        return None
    before = question_tokens[max(0, index - 2):index]
    return COMPLEMENTS[operator] if "not" in before or before[-1:] == ("no",) else operator


def nearby_operator(question_tokens: tuple[str, ...], index: int) -> str:
    """The comparison the words around the number at ``index`` ask for, '=' when none does. A comparative before "than"
    just before it sets it (``comparative_operator``); otherwise the cue nearest the number does, so in "before 1945
    and after 1955" 1955 is after. Only six comparatives and the cue first in a fixed order once set it: 1955 was
    before, "no less than 3000" less, and "not higher than 4" equal (Spider DEV 1028, 153 and 413, 2026-10-10)."""
    before = question_tokens[max(0, index - 4):index]
    after = question_tokens[index + 1:index + 3]
    if before[-1:] == ("than",) and len(before) >= 2:
        operator = comparative_operator(question_tokens, index - 2)
        if operator is not None:
            return operator
    cue = next((_COMPARISON_CUES[word] for word in reversed(before) if word in _COMPARISON_CUES), None)
    if cue is not None:
        return cue
    if after == ("or", "more") or after in {("or", "after"), ("or", "later")}:
        return ">="
    if after in {("or", "before"), ("or", "earlier")}:
        return "<="
    # "excluding Person_ID 2", "other than ID 3": every row but the one holding it (review F5, 2026-10-11, where the
    # 2 was read as an age).
    if set(before) & EXCLUDING_WORDS or any(pair == ("other", "than") for pair in zip(before, before[1:])):
        return "!="
    return "="


def count_admits(operator: str, bound: Any, count: int) -> bool:
    """Whether a number of rows ``count`` meets ``operator`` ``bound``: "fewer than 2" admits 0 and 1."""
    return {"<": count < bound, "<=": count <= bound, ">": count > bound, ">=": count >= bound,
            "=": count == bound, "!=": count != bound, "<>": count != bound}.get(operator, False)


# Words before a bare number that make it a count of related rows: "students who have 2 pets".
_COUNT_RELATION_CUES = frozenset({
    "contain", "contains", "conduct", "conducted", "design", "designed", "enroll", "enrolled",
    "has", "have", "having", "make", "operate", "perform", "performed", "produce", "shared",
    "teach", "used", "visit", "visited", "with",
})


def count_thresholds(tokens: tuple[str, ...]) -> list[CountThreshold]:
    """The thresholds the question states on a number of rows: "more than 2", "at most 3", "between 2 and 4", "2 or
    more", "exactly 0", a bare number after "has" or "with"; the strongest four, the counted noun at ``end``."""
    out = []
    for index in range(len(tokens)):
        if tokens[index] == "between" and index + 3 < len(tokens):
            low = parse_number(tokens[index + 1])
            high = parse_number(tokens[index + 3]) if tokens[index + 2] in {"and", "to"} else None
            if low is not None and high is not None:
                out.append(CountThreshold(((">=", int(low)), ("<=", int(high))), index, index + 4, 4.0))
        patterns = [
            (("at", "least"), ">=", 3.5), (("at", "most"), "<=", 3.5),
            (("more", "than"), ">", 3.0), (("greater", "than"), ">", 3.0),
            (("fewer", "than"), "<", 3.0), (("less", "than"), "<", 3.0),
            (("no", "more", "than"), "<=", 3.5), (("exactly",), "=", 3.0),
            (("only",), "=", 2.5), (("over",), ">", 2.0),
        ]
        for cue, operator, strength in patterns:
            if tokens[index:index + len(cue)] != cue:
                continue
            number_index = index + len(cue)
            if number_index >= len(tokens):
                continue
            number = parse_number(tokens[number_index])
            if number is not None:
                out.append(CountThreshold(((operator, int(number)),), index, number_index + 1, strength))
        number = parse_number(tokens[index])
        if number is not None and tokens[index + 1:index + 3] == ("or", "more"):
            out.append(CountThreshold(((">=", int(number)),), index, index + 3, 3.5))
        if number is not None and tokens[index + 1:index + 3] == ("or", "fewer"):
            out.append(CountThreshold((("<=", int(number)),), index, index + 3, 3.5))
        if number is not None:
            context = set(tokens[max(0, index - 3):index])
            explicit = context & {"at", "least", "most", "more", "greater", "fewer", "less", "than", "over", "only", "exactly"}
            if context & _COUNT_RELATION_CUES and not explicit:
                out.append(CountThreshold((("=", int(number)),), index, index + 1, 1.0))
    # One reading of each number: "more than 1 car maker and more than 1 airport" states two, which were one (the
    # review of 6263502, S3).
    dedup = {}
    for threshold in out:
        key = (threshold.predicates, threshold.end)
        old = dedup.get(key)
        if old is None or threshold.strength > old.strength:
            dedup[key] = threshold
    return sorted(dedup.values(), key=lambda item: (-item.strength, item.start))[:4]


def counted_thresholds(question_tokens: tuple[str, ...],
                       tables: Iterable[str]) -> tuple[tuple[CountThreshold, str], ...]:
    """(threshold, table) for each threshold on a number of rows a table's name then names: "countries with fewer
    than 2 car makers" counts car_makers' rows, and "exactly 0 car makers" too, which was answered by a car maker's
    Country key equal to 0 (the review of 2026-10-11, R1). Of the readings of one number ("no more than 3" also says
    "more than 3") the strongest is kept; two numbers alike are two thresholds ("more than 1 car maker and more than 1
    airport", the review of 6263502, S3)."""
    names = sorted(((name_tokens(table), table) for table in tables), key=lambda item: -len(item[0]))
    by_end: dict[int, tuple[CountThreshold, str]] = {}
    for threshold in count_thresholds(question_tokens):
        table = next((table for name, table in names
                      if name and question_tokens[threshold.end:threshold.end + len(name)] == name), None)
        held = by_end.get(threshold.end)
        if table is not None and (held is None or threshold.strength > held[0].strength):
            by_end[threshold.end] = (threshold, table)
    return tuple(sorted(by_end.values(), key=lambda item: item[0].start))


def count_requirement_operator(
    question_tokens: tuple[str, ...], requirements: Sequence[tuple[CountThreshold, str]]
) -> str | None:
    """The shared connective of explicitly coordinated count requirements; mixed/unbound scope is unresolved.

    Read between occurrences, excluding each threshold's own "two or more" or range. A count's presence alone
    cannot prove whether its population intersects or joins the other count's population.
    """
    operators = set()
    for (left, _), (right, _) in zip(requirements, requirements[1:]):
        said = set(question_tokens[left.end:right.start]) & {"and", "or"}
        if len(said) != 1:
            return None
        operators.update(said)
    return next(iter(operators)).upper() if len(operators) == 1 else None


def threshold_number(question_tokens: tuple[str, ...], index: int) -> bool:
    """Whether the number at ``index`` is a threshold a comparison takes ("more than two", "at least 3", "exactly
    2"), not a count of rows to keep."""
    before = tuple(question_tokens[max(0, index - 2):index])
    return (nearby_operator(question_tokens, index) != "=" or before in {("at", "least"), ("at", "most")}
            or before[-1:] == ("exactly",))


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


def groups_requested(question: str) -> bool:
    """Whether the question asks for a value in each group: "each", "per", "every", a "by" that groups
    (``by_groups``), or "for different" ("the numbers of wines for different grapes"). "How many countries have
    more than 2 car makers" asks for none: it counts the countries that qualify."""
    question_tokens, raw = tokens(question), words(question)
    return any(token in {"each", "per", "every"} or by_groups(question_tokens, raw, index)
               or (token == "different" and index > 0 and question_tokens[index - 1] in {"for", "in", "across"})
               for index, token in enumerate(question_tokens))


# A comparative before "average" compares rows with their average (canon() forms).
_ABOVE_AVERAGE = re.compile(r"\b(?:older|above|greater|higher|more)\s+(?:than\s+)?(?:the\s+)?(?:average|avg|mean)\b")
_BELOW_AVERAGE = re.compile(r"\b(?:younger|below|less|lower|fewer)\s+(?:than\s+)?(?:the\s+)?(?:average|avg|mean)\b")


def average_comparison(question: str) -> str | None:
    """The operator, ">" or "<", by which the question compares rows with their average: "singers older than average",
    "dogs with an age below the average". The search builds the comparison with the average a scalar subquery
    computes (``RecursiveQueryExpander``), and the completeness check reads that average as the one the question asks
    for. None when the question compares nothing with an average."""
    normalized = " ".join(tokens(question))
    if _ABOVE_AVERAGE.search(normalized):
        return ">"
    if _BELOW_AVERAGE.search(normalized):
        return "<"
    return None


def field_numbers(question: str, fields: Iterable[Any]) -> frozenset[int]:
    """The positions in ``tokens(question)`` of a "number" that ends the name of a field: the word before it ends
    the field's name, or comes before the name's own last word "number" ("the phone number" of Phone, "flight
    numbers" of flight_number, "the staff number" of Num_of_Staff). It asks for the field, not for a count: "the
    name and phone number of these patients" showed each patient's count of appointments (held-out Spider train 47,
    2026-10-11). An owner's number ("each customer's number of orders") and a quantity's ("the total number", "the
    largest number") count. A "number" that begins a field's name, the rest of the name following it, asks for the
    field too: "the average and maximum number of tickets" of Num_of_Ticket, "the number of products" of
    Number_products (Spider DEV 271, 424: each was counted). So does a "number of" before a numeric field's whole
    name: "in ascending order of number of police officers" of Police_officers, "the maximum number of floors" of
    Floors (held-out Spider train 30, 633, 2026-10-12: each was counted; the gold of 5 such DEV questions and 15 of 16
    held-out ones reads the field). A text field's name stays a count: "the number of countries" beside Country.

    ``fields`` are names, column references (a name and a type) or planner schema entries ({name, affinity}); a name
    alone says no type, so it reads no numeric field."""
    from engine.closed_class import DEGREE_MODIFIERS
    from engine.sql_extrema import MAX_CUES, MIN_CUES
    quantities = FUNCTION_WORDS | DEGREE_MODIFIERS | MAX_CUES | MIN_CUES | {"total", "sum", "average", "avg", "mean",
                                                                              "count"}
    ends = set()
    rests = set()
    measures = set()
    tables = set()
    for field in fields:
        if isinstance(field, dict):
            name, numeric = field.get("name", ""), field.get("affinity") in ("INTEGER", "REAL")
            table = field.get("table")
        else:
            name = getattr(field, "name", field)
            numeric = bool(getattr(getattr(field, "type", None), "numeric", False))
            table = getattr(field, "table", None)
        if table:
            tables.add(name_tokens(table)[-1:])
        name_words = name_tokens(name)
        if len(name_words) > 1 and name_words[-1] in {"number", "no", "num"}:
            ends.add(name_words[-2])
        elif name_words:
            ends.add(name_words[-1])
        if len(name_words) > 1 and name_words[0] in {"number", "no", "num"}:
            rests.add(tuple(word for word in name_words[1:] if word != "of"))
        elif numeric and name_words and not is_surrogate_key(str(name)):
            measures.add(name_words)
    raw, question_tokens = words(question), tokens(question)

    def begins_a_name(index):
        following = [token for token in question_tokens[index + 1:index + 6] if token not in {"of", "the"}]
        return any(rest and tuple(following[:len(rest)]) == rest for rest in rests)

    def names_a_measure(index):
        # A table's name counts its rows, whatever field shares it: "the number of continents" beside the integer key
        # countries.Continent, "the number of airlines" beside flights.Airline (Spider DEV 88, 188).
        if question_tokens[index + 1:index + 2] != ("of",):
            return False
        following = tuple(token for token in question_tokens[index + 2:index + 6] if token != "the")
        return following[:1] not in tables and any(following[:len(measure)] == measure for measure in measures)

    return frozenset(index for index, token in enumerate(question_tokens)
                     if token == "number" and ((index and question_tokens[index - 1] in ends - quantities
                                                and not raw[index - 1].endswith("'s")) or begins_a_name(index)
                                               or names_a_measure(index)))


def count_requested(question: str, fields: Iterable[Any]) -> bool:
    """Whether the question asks for a count: "count", "how many", or a "number" that names none of the ``fields``
    (``field_numbers``)."""
    question_tokens = tokens(question)
    labels = field_numbers(question, fields)
    return "count" in question_tokens or "how many" in " ".join(question_tokens) or any(
        token == "number" and index not in labels for index, token in enumerate(question_tokens))


def explicit_order(question_tokens: tuple[str, ...]) -> bool:
    return bool(set(question_tokens) & {"order", "ordered", "sort", "sorted", "top", "bottom"})


def parse_number(token: str):
    if token in WORD_NUMBERS:
        return WORD_NUMBERS[token]
    if NUMBER_TEXT.match(token):
        token = token.replace(",", "")
        return parse_decimal(token) if "." in token else int(token)
    return None


# The prepositions that open a relative clause with "which": "for which", "in which", "from which".
_RELATIVE_LEADS = frozenset({"for", "in", "on", "from", "of", "by", "at", "with", "to", "into", "during", "through"})


def projection_window(question_tokens: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
    commands = {"find", "give", "list", "return", "show", "what", "which"}
    boundaries = {
        "has", "have", "having", "shared", "that", "under", "where", "which",
        "who", "whose", "with",
    }
    # A "which" after a preposition opens a relative clause, not a command: "Return the nationalities for which there
    # are two or more people" shows the nationalities (Spider DEV 678, 2026-10-15).
    late_commands = [
        index for index, token in enumerate(question_tokens)
        if token in commands and index > 2 and question_tokens[index - 1] not in _RELATIVE_LEADS
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


# Words that ask for an aggregate of the rows (canon() forms), and the aggregate each asks for.
AGGREGATE_CUES = {"average": "AVG", "avg": "AVG", "mean": "AVG", "sum": "SUM", "total": "SUM",
                  "maximum": "MAX", "max": "MAX", "minimum": "MIN", "min": "MIN"}

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


def entity_words(schema: Any) -> frozenset[str]:
    """The words of the schema's table names (canon() forms), read once per schema."""
    entities = _ENTITY_WORDS.get(schema)
    if entities is None:
        entities = _ENTITY_WORDS[schema] = frozenset(word for table in schema.tables for word in name_tokens(table))
    return entities


_ENTITY_WORDS: "weakref.WeakKeyDictionary[Any, frozenset[str]]" = weakref.WeakKeyDictionary()


def naming_context(schema: Any) -> dict[str, frozenset[str]]:
    """For each table, the words that say what none of its columns holds alone: the words of the tables'
    names ("the treatment type description" names no treatment_type_code) and the words two of its columns'
    names share ("tourney" of tourney_name and tourney_date, Spider wta_1, 2026-10-02). Read once per
    schema."""
    context = _NAMING_CONTEXTS.get(schema)
    if context is None:
        entities = entity_words(schema)
        context = {}
        for table in schema.tables:
            names = {column.ref.name for column in schema.by_table.get(table, ())}
            shared = Counter(word for name in names for word in set(name_tokens(name)))
            context[table] = entities | frozenset(word for word, count in shared.items() if count > 1)
        _NAMING_CONTEXTS[schema] = context
    return context


# The words a measure's name ends in that say its unit or scale, not what it measures (canon forms): "the maximum
# net worth" is Net_Worth_Millions, "the sales" Sales_in_Billion, "the wind speed" wind_speed_mph.
UNIT_WORDS = frozenset({"million", "billion", "thousand", "percent", "pct", "mph", "inch", "feet", "foot", "ft",
                        "dollar", "usd", "km", "kg", "meter", "metre", "mm", "cm"})


def naming_words(link_words: tuple[str, ...], context: frozenset[str]) -> tuple[str, ...]:
    """The words of a column's name a question must say to name the column: all but the words of its
    kind, when the others say what its values are ("email" names email_address): no function word ("of" of
    date_of_treatment) and none of ``context``, its table's naming_context. A measure's trailing unit, with an "in"
    before it, is left unsaid as well: "Show different citizenships and the maximum net worth of singers of each
    citizenship" asks for Net_Worth_Millions (Spider DEV 1016, 1017, 2026-10-17), unless the words left are shared
    with another field of the table (``context``): "the length" of length_feet and length_meters names neither."""
    measured = link_words
    if len(measured) > 1 and measured[-1] in UNIT_WORDS:
        measured = measured[:-1]
        if len(measured) > 1 and measured[-1] == "in":
            measured = measured[:-1]
    content = tuple(word for word in measured if word not in KIND_WORDS)
    if content and not set(content) & (FUNCTION_WORDS | context):
        return content
    return link_words


# The parts of a name a field abbreviates by its initial (``name_part_words``).
_NAME_PART_INITIALS = {"f": "first", "l": "last", "m": "middle"}


def name_part_words(name: str) -> tuple[str, str] | None:
    """The words of the part of a person's name a field holds: "Fname", "firstname", "STU_FNAME" and "name_first" are
    the first name, "LName" and "emp_lname" the last (Spider pets_1, college_1, baseball_1). None for any other field.
    The search links "the first name" to such a field by them, and the completeness check reads them."""
    from engine.sql_schema import name_words
    parts = [word.lower() for word in name_words(name)]
    if len(parts) >= 2 and parts[-2] == "name" and parts[-1] in {"first", "last", "middle"}:
        return (parts[-1], "name")
    for token in (parts[-1] if parts else "", "".join(parts)):
        head = token[:-4] if token.endswith("name") else None
        part = _NAME_PART_INITIALS.get(head) or (head if head in {"first", "last", "middle"} else None)
        if part:
            return (part, "name")
    return None


# The stems of the operations a query performs: a word sharing one names the operation, which the query must realize,
# not a table or field it reads ("ordered by" an Orders table, "listed" a Listings one).
OPERATION_STEMS = frozenset({"order", "sort", "group", "list", "count", "total", "sum", "rank", "name", "number",
                             "show", "return", "find", "give", "select", "averag", "distinct", "differ", "includ",
                             "contain", "start", "end"})


def word_stems(word: str) -> set[str]:
    """``word`` and its forms without one inflectional or derivational suffix, a doubled final consonant undone, of
    four letters or more: "enrolled" -> enroll, enrol; "enrolment" -> enrol; "populated" -> populat, populate. A "d"
    alone ends only a past form ("created" -> create): "amenid" has no stem "ameni" to say "amenity" (held-out Spider
    train 531, 2026-10-13)."""
    out = {word}
    for suffix in ("ing", "ed", "d", "es", "s", "ment", "er", "or", "ion"):
        if suffix == "d" and not word.endswith("ed"):
            continue
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            stem = word[:-len(suffix)]
            out.add(stem)
            if len(stem) > 4 and stem[-1] == stem[-2]:
                out.add(stem[:-1])
    return {stem for stem in out if len(stem) >= 4}


def says_in_another_form(word: str, name_word: str, entities: frozenset[str] = frozenset()) -> bool:
    """Whether the question's ``word`` says ``name_word``, a word of a table's or field's name, in another form: they
    share a stem of five letters or more ("enrolled" Student_Enrolment, "arriving" date_arrived, "directors"
    directed_by), or one is the other's abbreviation (``spells_out``: "account balance" acc_bal, "latitude" lat,
    "institution" Inst) and the word spelling it out names no table (``entity_words``: "students" spells out no
    ENROLL.STU_NUM, the abbreviation of a table's name being read beside its table, ``abbreviated_table``; held-out
    Spider train 58, 557, 938, 2026-10-16: three right answers showed it), and the word names no operation ("ordered"
    by an Orders table). Both are canon() forms. The field linker (sql_search) names fields by it and the completeness gate
    (query_contract) reads words by it."""
    if not word.isalpha():
        return False
    stems = word_stems(word)
    if any(stem.startswith(operation) or operation.startswith(stem) for stem in stems for operation in OPERATION_STEMS):
        return False
    abbreviation = ((word not in entities and spells_out(word, name_word))
                    or (name_word not in entities and spells_out(name_word, word)))
    return abbreviation or any(
        mine.startswith(theirs) or theirs.startswith(mine)
        for mine in stems for theirs in word_stems(name_word) if min(len(mine), len(theirs)) >= 5)


# The endings that make a word another form of the word they follow, not the word it abbreviates: "longest" is no
# longitude, "nameless" no name.
_FORM_ENDINGS = frozenset({"s", "es", "ed", "d", "er", "r", "est", "ing", "ers", "ly", "y", "less", "ness", "ful"})
# Ordering words name no field: "descending" spells out no ClubDesc, "ascending" no asc field, "according to" no
# acc_bal.
_ORDERING_STARTS = ("ascend", "descend", "accord")


def spells_out(word: str, short: str) -> bool:
    """Whether ``word``, of seven letters or more, spells out ``short``, a field's or table's name word of three or four
    letters that begins it and is no stop word: "account" acc, "balance" bal, "enrollment" enr, "longitude" long,
    "institution" inst (held-out Spider train 202, 343, 369, 292 and the fresh holdout, 2026-10-16: 35 questions saying
    such a field's words in full were refused). A stop word abbreviates nothing ("percentage" is no
    Residents_per_officer), and a form of the short word itself ("longest", "stayed") and an ordering word
    ("descending") spell out none. Both are canon() forms."""
    return (3 <= len(short) <= 4 and len(word) >= 7 and short.isalpha() and word.startswith(short)
            and word[len(short):] not in _FORM_ENDINGS and not word.startswith(_ORDERING_STARTS)
            and not _stop_word(short))


@lru_cache(maxsize=4096)
def _stop_word(word: str) -> bool:
    return bool(spacy_model().vocab[word].is_stop)


def abbreviated_table(name: str, table: str) -> bool:
    """Whether the first word of the field ``name`` abbreviates a word of its ``table``'s name: cust_name of customer,
    crs_description of COURSE, StuID of Student; not a name's part (Fname) nor the table's word itself (customer_name).
    The search links such a field beside its table (sql_search._column_link_positions) and column_matches reads it."""
    if name_part_words(name):
        return False
    words = [canon(word) for word in name_words(name)]
    table_words = {canon(word) for word in name_words(table)}
    return len(words) > 1 and words[0] not in table_words and any(abbreviates(words[0], word) for word in table_words)


def abbreviates(short: str, full: str) -> bool:
    """Whether ``short`` abbreviates the word ``full``: a shorter start of it ("cust" customer, "f" file), or of three
    letters or more its letters in order from its first ("crs" course, "dept" department, "apt" apartment)."""
    if not short.isalpha() or len(short) >= len(full) or short[0] != full[0]:
        return False
    if full.startswith(short):
        return True
    rest = iter(full)
    return len(short) >= 3 and all(letter in rest for letter in short)


def column_matches(name: str, question_tokens: set[str], table: str | None = None,
                   context: frozenset[str] | None = None) -> bool:
    """Whether the question names the column ``name``: its words, or with its table's naming_context its
    words but those of its kind (naming_words)."""
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if compact in question_tokens:
        return True
    special = {
        "sex": ({"sex", "gender"},), "gender": ({"sex", "gender"},),
        "mpg": ({"mpg", "mile"}, {"mpg", "gallon"}),
    }
    parts = name_part_words(name)
    groups = tuple({word} for word in parts) if parts else special.get(compact)
    if groups is None:
        aliases = {
            "maker": {"maker", "manufacturer"},
            "weight": {"weight", "weigh", "weighed", "weighing"},
            "accelerate": {"accelerate", "acceleration"},
            "hometown": {"hometown", "town", "city"},
            "death": {"death", "killed"},
        }
        table_words = set(name_tokens(table)) if table is not None else set()
        # A first word abbreviating the table's name is that table's word, where the question says the table: "the id
        # of students" names StuID (Spider DEV 63, 64, 2026-10-13: the students' last names were shown), and "the first
        # names of all professors" no DEPT_NAME of a department it never says (fresh holdout of Spider train 183).
        abbreviation = (table is not None and abbreviated_table(name, table)
                        and bool(table_words & question_tokens))
        words = [
            word for index, word in enumerate(name_tokens(name))
            if word != "of" and (word not in table_words or len(name_tokens(name)) == 1)
            and not (abbreviation and index == 0)
        ]
        if context is not None:
            words = list(naming_words(tuple(words), context))
        groups = tuple(aliases.get(word, {word}) for word in words)
    return bool(groups) and all(bool(group & question_tokens) for group in groups)


def semantic_tokens(name: str) -> tuple[str, ...]:
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    special = {"sex": ("sex", "gender"), "gender": ("sex", "gender")}
    words = list(name_part_words(name) or special.get(compact, name_tokens(name)))
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
# A form of "to be" between a noun and a comparison says it of that noun's rows: "orders are over 20".
COPULAS = frozenset({"is", "are", "was", "were"})
# The words between a field's name and the number it is compared with: "a total amount of more than 10", "an
# average grade no less than 80", "at least 5".
THRESHOLD_WORDS = frozenset({"than", "of", "over", "above", "under", "below", "at", "least", "most", "no", "not",
                              "exceeding", "exceeds", "or", "equal", "to", *QUANTITY_COMPARATIVES, *COMPARATIVES})
# The words before an aggregate a thing has: "customers with a total amount over 10", "whose average grade".
_HOLDING_WORDS = frozenset({"with", "have", "has", "had", "having", "whose", "where"})


def money_total_position(question_tokens: Sequence[str], table_words: Iterable[str], count_asked: bool) -> int | None:
    """Position of a money noun that names a table and asks for its money total, else None.

    Tokens and table words are canon() forms; ``count_asked`` is the question's ``count_requested``. Both planners
    apply this one rule, the own-data search (sql_search) and the world-join operand choice
    (encoder_overlay.read_op_all); each still requires a money measure column in that table, without which the noun
    keeps its entity reading.
    """
    question_tokens = tuple(question_tokens)
    if count_asked or ROW_LISTING_COMMANDS & set(question_tokens):
        return None
    names = set(table_words)
    # A number before the noun counts it: "customers with over 2 sales" totalled the sales' amount (2026-10-10).
    return next((index for index, token in enumerate(question_tokens)
                 if token in MONEY_MEASURE_NOUNS and token in names
                 and not (index and parse_number(question_tokens[index - 1]) is not None)), None)


def money_total_columns(question: str, sch: Sequence[dict]) -> tuple[str, list[dict]] | None:
    """(table, its money-named numeric columns) when ``question`` asks for a money-named table's total.

    ``sch`` is the planner schema ({table, name, affinity, ...}). None when the rule does not fire
    or the table has no money column.
    """
    question_tokens = tokens(question)
    table_of = {word: entry["table"] for entry in sch for word in name_tokens(entry["table"])}
    position = money_total_position(question_tokens, table_of,
                                    count_requested(question, sch))
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


def compatible_types(left: SQLType, right: SQLType) -> bool:
    return left == right or (left.numeric and right.numeric)


def name_tokens(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(canon(token) for token in re.findall(r"[^\W_]+", spaced, re.UNICODE))


def spelled_names(question_tokens: tuple[str, ...], schema: Any) -> frozenset[int]:
    """The positions where the question spells a several-word column's name, its words together: "the first
    and last name" holds no value 'Last', and "the avg. monthly searches" names the column Avg. monthly
    searches, not an average of it."""
    phrases = set()
    for column in schema.columns:
        phrase = name_tokens(column.ref.name)
        phrases.add(phrase)
        # Spreadsheet headers commonly abbreviate "average" as "Avg.". Treat those
        # spellings as the same column mention so "sum of average monthly searches"
        # does not also interpret "average" as a second aggregate operation.
        for index, token in enumerate(phrase):
            if token in {"avg", "average"}:
                phrases.add((*phrase[:index], "average" if token == "avg" else "avg", *phrase[index + 1:]))

    # A word the name abbreviates spells it too: "the name, independence year, and surface area" names IndepYear and
    # holds no city 'Independence' (Spider DEV 788, 789, 2026-10-17: both filtered on that city).
    def spells(said, words):
        return all(word == spelled or (len(word) >= 3 and abbreviates(word, spelled)) for spelled, word in zip(said, words))

    return frozenset(index for words in phrases if len(words) > 1
                     for start in range(len(question_tokens) - len(words) + 1)
                     if spells(question_tokens[start:start + len(words)], words)
                     for index in range(start, start + len(words)))


# The words before "the" that make an extreme select the row holding it (``selects_extreme``).
_SELECTOR_LEADS = frozenset({"with", "has", "have", "had", "having"})
_EXTREME_WORDS = {"maximum": "MAX", "max": "MAX", "minimum": "MIN", "min": "MIN"}


def selects_extreme(question_tokens: Sequence[str], position: int) -> bool:
    """Whether the extreme at ``position`` ("maximum", "minimum") selects the row holding it, as "the highest" does,
    rather than asking for the extreme itself: "the country with the maximum GNP", "Which model of the car has the
    minimum horsepower?" (Spider DEV 95, 2026-10-15: 11 such questions, none answered right)."""
    return (question_tokens[position] in _EXTREME_WORDS and position >= 2 and question_tokens[position - 1] == "the"
            and question_tokens[position - 2] in _SELECTOR_LEADS)


def selected_extremes(question_tokens: Sequence[str]) -> dict[str, str]:
    """For each extreme ("MAX", "MIN") every mention of which selects the row holding it (``selects_extreme``), the
    direction an ordering realizing it sorts in."""
    out = {}
    for function, direction in (("MAX", "DESC"), ("MIN", "ASC")):
        positions = [index for index, token in enumerate(question_tokens) if _EXTREME_WORDS.get(token) == function]
        if positions and all(selects_extreme(question_tokens, position) for position in positions):
            out[function] = direction
    return out


def asked_cues(cues: list[tuple[str, int]], question_tokens: tuple[str, ...],
               schema: Any) -> list[tuple[str, int]]:
    """The aggregate cues (function, position) that ask for their aggregate, as the search and the ranker read
    them. A cue inside a several-word column name the question spells (``spelled_names``) names that column: it
    asks only when no other cue does, and an average never does, since an average of rows' averages is not
    their average: "the Avg. monthly searches for all Keyword containing 'inspection checklist'" lists the 38
    keywords' searches rather than averaging them (a customer's keyword sheet, 2026-10-06). The total, maximum
    or minimum of rows' totals, maxima or minima is theirs: "the total amount in Paris" sums Total Amount. An
    extreme that selects the row holding it asks for no aggregate (``selects_extreme``)."""
    cues = [(function, position) for function, position in cues if not selects_extreme(question_tokens, position)]
    spelled = spelled_names(question_tokens, schema)
    if any(position not in spelled for _, position in cues):
        return [(function, position) for function, position in cues if position not in spelled]
    return [(function, position) for function, position in cues if function != "AVG"]


# A "%" standing alone is the word "percent" ("what % of the total amount comes from Paris" served the
# Paris total, 2026-10-02); after a number ("over 50%") it is the number's unit, and inside a word or
# quotes ("names like 'A%'") a pattern's wildcard.
_QUESTION_WORD = re.compile(NUMBER_WORD + r"|[^\W_]+(?:'[^\W_]+)?|(?<![\w%'\"])(?<!\d\s)%(?![\w'\"])", re.UNICODE)


# Typographic apostrophes and quotes, and the replacement character a mis-decoded one leaves, as the ASCII marks the
# readers of a question parse (``plain_quotes``).
_TYPOGRAPHIC_MARKS = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"',
                                    "�": "'"})


def plain_quotes(question: str) -> str:
    """``question`` with its typographic apostrophes and quotes written as the ASCII ones: "Brazil’s population" said
    "Brazil" and a lone "s", and “Smith” quoted nothing (Spider DEV 715, 719, held-out 202, 2026-10-16: each was
    refused). Selection reads a question so (engine/tables.py:TableQuery.select_query)."""
    return str(question).translate(_TYPOGRAPHIC_MARKS).replace("``", '"').replace("''", '"')


def words(text: str) -> list[str]:
    """The lowercase words of a question, before any module's own normalization: the one word
    splitter the search, its expansions and the ranker read a question with."""
    return ["percent" if word == "%" else word for word in _QUESTION_WORD.findall(text.lower())]


def word_spans(text: str) -> list[tuple[int, int]]:
    """Where each of ``words(text)`` stands in ``text``."""
    return [match.span() for match in _QUESTION_WORD.finditer(text.lower())]


@dataclass(frozen=True)
class OutputRequest:
    """One output an explicit list instruction asks to show, in the order asked: the span of ``tokens(question)`` that
    says it, and whether it asks for a count ("the number of countries") rather than a field."""
    start: int
    end: int
    count: bool


# The verbs that open a sentence listing the outputs ("List the continent id, continent name and the number of
# countries."), the words before the list that ask nothing, and the words that end it.
_LIST_VERBS = frozenset({"list", "show", "give", "return", "report", "display", "tell", "provide", "output", "find"})
_LIST_LEADS = frozenset({"only", "me", "us", "both", "the", "a", "an", "all", "please", "just", "also", "each", "every",
                         "his", "her", "their", "its", "different", "distinct", "unique"})
_LIST_ENDS = frozenset({"for", "who", "that", "which", "whose", "where", "when", "with", "in", "from", "by", "order",
                        "ordered", "sort", "sorted", "rank", "ranked", "if", "than"})
_LIST_COUNTS = frozenset({"number", "count", "many", "total"})


# The words before a sentence's command that ask nothing ("Please list", "and show").
_INSTRUCTION_LEADS = frozenset({"please", "only", "also", "and", "then", "now"})


def instruction_starts(question: str) -> tuple[int, ...]:
    """Where in ``tokens(question)`` an instruction may open: each sentence, and a clause after a comma that a list verb
    opens ("For each continent, list its id, name, ...", "..., and return the name, location and district of each
    shop")."""
    spans = word_spans(question)
    said = tokens(question)
    if len(spans) != len(said):
        return ()
    gaps = [question[spans[index - 1][1]:spans[index][0]] if index else "" for index in range(len(spans))]
    starts = [0]
    for index in range(1, len(said)):
        if re.search(r"[.?!;]", gaps[index]):
            starts.append(index)
        elif "," in gaps[index]:
            verb = index
            while verb < len(said) and said[verb] in _INSTRUCTION_LEADS:
                verb += 1
            if (verb < len(said) and said[verb] in _LIST_VERBS
                    and not re.search(r"[.?!;,]", "".join(gaps[index + 1:verb + 1]))):
                starts.append(index)
    return tuple(starts)


def output_requests(question: str, schema: Any = None) -> tuple[OutputRequest, ...]:
    """The outputs a sentence after the first asks for by an instruction, in order: "How many countries does each
    continent have? List the continent id, continent name and the number of countries." asks for the continent id, the
    continent name and the count, and "Which makers designed more than 3 car models? List full name and the id." for
    the full name and the id (Spider DEV 89, 149). So does a clause a list verb opens after a comma: "For each
    continent, list its id, name, and how many countries it has?", "Sort all the shops by number products ..., and
    return the name, location and district of each shop." (Spider DEV 90, 274; instruction_starts). The search builds
    that projection and the ranker reads whether an answer shows exactly those, one reading for both. Empty where no
    such sentence lists them.

    An item counts when it begins with the count ("the number of countries", "how many"), unless its "number" names
    one of ``schema``'s fields (field_numbers): "Tell me the name and phone number of these patients" asks for the
    phone (held-out Spider train 47)."""
    spans = word_spans(question)
    said = tokens(question)
    if len(spans) != len(said):
        return ()
    numbers = field_numbers(question, (column.ref for column in schema.columns)) if schema is not None else frozenset()
    gaps = [question[spans[index - 1][1]:spans[index][0]] if index else "" for index in range(len(spans))]
    for start in instruction_starts(question):
        index = start
        while index < len(said) and said[index] in _INSTRUCTION_LEADS:
            index += 1
        # The opening sentence lists outputs when it asks for two or more, coordinated, by a command or "what is/are":
        # "What are the names, themes, and number of singers for every concert?", "the names and areas of countries"
        # (Spider DEV 34, 800, 2026-10-14). Its list ends where the rows it reads begin ("of countries"), which no
        # item names; a later sentence's list may end with its owner ("the number of countries").
        opening = start == 0
        if said[index:index + 1] in (("what",), ("which",)) and said[index + 1:index + 2] in (
                ("is",), ("are",), ("was",), ("were",)) and opening:
            index += 1
        elif index >= len(said) or said[index] not in _LIST_VERBS:
            continue
        index += 1
        end = next((position for position in range(index, len(said))
                    if position > index and re.search(r"[.?!;]", gaps[position])), len(said))
        requests: list[OutputRequest] = []
        item = index
        position = index
        # "along with", "together with" and "as well as" add an item, as "and" does, even past the rows the items
        # before them name: "the full name of each car maker, along with its id and how many models it produces"
        # (Spider DEV 94, 2026-10-15).
        joined = {later: _joins_items(said, later) for later in range(index, end) if _joins_items(said, later)}
        inside = {later + offset for later, length in joined.items() for offset in range(length)}

        def ends_at(start):
            return next((later for later in range(start, end) if said[later] in _LIST_ENDS and later not in inside),
                        end)

        stop = ends_at(index)
        while position < end:
            word = said[position]
            if position in joined and position > item:
                requests.append(_output_request(said, item, position, numbers))
                position += joined[position]
                item = position
                stop = ends_at(position)
                continue
            boundary = position > item and ("," in gaps[position] or word in {"and", "&"})
            # In the opening sentence "of" ends the list where no item follows it: "the names and areas of countries",
            # while "the date of account opened, the account name, and ..." goes on (held-out Spider train 113).
            ends_items = opening and not any("," in gaps[later] or said[later] in {"and", "&"} or later in joined
                                             for later in range(position + 1, stop))
            if (word in _LIST_ENDS and position not in inside) or (
                    word == "of" and position > item and said[position - 1] not in _LIST_COUNTS
                    and (ends_items or said[position + 1:position + 2] in (("each",), ("every",), ("all",)))):
                resume = next((later for later in sorted(joined) if later > position), None)
                if resume is None:
                    break
                requests.append(_output_request(said, item, position, numbers))
                position = item = resume + joined[resume]
                stop = ends_at(position)
                continue
            if boundary:
                requests.append(_output_request(said, item, position, numbers))
                item = position + (1 if word in {"and", "&"} else 0)
            position += 1
        requests.append(_output_request(said, item, position, numbers))
        found = [request for request in requests if request is not None]
        # A bare noun added to a count by "and" is counted too: "the number of different series names and contents"
        # counts both (Spider DEV 597, 2026-10-15), while "the number of employees ... and the name of the shop" asks
        # for the name.
        for position in range(1, len(found)):
            joined = said[found[position].start - 1:found[position].start] in (("and",), ("&",))
            if found[position - 1].count and joined:
                found[position] = OutputRequest(found[position].start, found[position].end, True)
        if found and (len(found) > 1 or not opening or (
                schema is not None and _one_closed_field(question, said, spans, gaps, found, schema, numbers))):
            return tuple(found)
    return ()


# The words between an output and the name of the rows it is read from ("the Episode of all TV series").
_OUTPUT_OWNER_LEADS = frozenset({"the", "all", "each", "every", "a", "an", "its", "their", "his", "her", "these", "those"})


def _one_closed_field(question, said, spans, gaps, found, schema, numbers) -> bool:
    """Whether an opening instruction asking for one item asks for exactly one field and nothing else: "List the
    Episode of all TV series showed on TV Channel with series name Sky Radio" shows the Episode, not also the channel
    key its join repeats (Spider DEV 625; the Codex review's single-field reader, 2026-10-16). The item must name one
    field the schema binds unambiguously, no table ("show countries" stays the general display reading), and no later
    item may follow it (list_closed). An owner phrase after it ("of all TV series") must name that field's own table and
    end there, at a verb, preposition or relative word, so "the book type of each book" is not a field of each book."""
    if len(found) != 1 or found[0].count:
        return False
    request = found[0]
    label = tuple(said[position] for position in range(request.start, request.end)
                  if position not in numbers and said[position] not in FUNCTION_WORDS | _LIST_LEADS)
    if not label or any(label in (name_tokens(table), name_tokens(table)[-1:]) for table in schema.tables):
        return False
    if not list_closed(question, found):
        return False
    bound = _bound_fields(question, schema, tuple(found))
    if len(bound) != 1 or len(bound[0]) != 1 or realizes_request(bound[0][0], said, request, schema, numbers) < 2:
        return False
    field = bound[0][0]
    if said[request.end:request.end + 1] not in (("of",), ("for",)):
        return label == (name_part_words(field.name) or name_tokens(field.name))
    owner_start = request.end + 1
    while owner_start < len(said) and said[owner_start] in _OUTPUT_OWNER_LEADS:
        owner_start += 1
    owner = name_tokens(field.table)
    boundary = owner_start + len(owner)
    if not owner or said[owner_start:boundary] != owner:
        return False
    if boundary >= len(said) or re.search(r"[.?!;]", gaps[boundary]):
        return True
    offset = spans[boundary][0]
    following = next((token for token in spacy_model()(question)
                      if token.idx <= offset < token.idx + len(token.text)), None)
    return following is not None and (following.pos_ in {"VERB", "AUX", "ADP", "SCONJ", "PRON", "ADV"}
                                      or following.tag_ in {"WDT", "WP", "WP$", "WRB"})


def list_closed(question: str, requests: Sequence[OutputRequest]) -> bool:
    """Whether an instruction's list of outputs ends before any later coordinated output: "the name and theme for all
    concerts and the number of singers" asks for another output after its owner phrase, so the parsed fields cannot
    stand for the whole answer. Only a closed list may remove an output no item asks for."""
    if not requests:
        return False
    said, spans = tokens(question), word_spans(question)
    if len(said) != len(spans):
        return False
    for index in range(requests[-1].end, len(said)):
        if index and re.search(r"[.!?;]", question[spans[index - 1][1]:spans[index][0]]):
            break
        if said[index] in {"and", "or", "also", "together", "plus"}:
            return False
    return True


def _joins_items(said: Sequence[str], position: int) -> int:
    """The length of the words at ``position`` that add an item to a list ("along with", "together with", "as well
    as", and "and" before a possessive: "the name and date for each race and its track name"), else 0."""
    if tuple(said[position:position + 2]) in (("along", "with"), ("together", "with")):
        return 2
    if said[position:position + 1] == ("and",) and said[position + 1:position + 2] in (
            ("its",), ("their",), ("his",), ("her",)):
        return 1
    return 3 if tuple(said[position:position + 3]) == ("as", "well", "as") else 0


def command_positions(question: str) -> frozenset[int]:
    """The positions in ``tokens(question)`` of the verb a sentence opens with ("Show", "Please list"), which the tagger
    reads as a verb: the command, naming no rows. "Please show the different record companies" asks for no show of an
    orchestra database's show table (Spider DEV 840, 846, 848, 2026-10-14: every reading joined the shows), and "For
    each continent, list its id ..." no model_list (instruction_starts)."""
    from engine.closed_class import action_words
    spans = word_spans(question)
    said = tokens(question)
    if len(spans) != len(said):
        return frozenset()
    verbs = {canon(word) for word in action_words(question)}
    found = set()
    for index in instruction_starts(question):
        while index < len(said) and said[index] in _INSTRUCTION_LEADS:
            index += 1
        if index < len(said) and said[index] in verbs:
            found.add(index)
    return frozenset(found)


# The words that open a verb's object: a word before one is no noun's head.
_OBJECT_OPENERS = frozenset({("a",), ("an",), ("the",), ("any",), ("some",), ("all",), ("every",), ("each",), ("their",),
                             ("its",), ("his",), ("her",), ("more",), ("at",)})


def counted_noun_tables(said: Sequence[str], schema: Any) -> frozenset[str]:
    """The tables whose rows a count counts: those the noun right after "how many", "number of" or "count of" names
    ("How many countries does each continent have?" counts the countries). The ranker credits a count of them, and a
    row count keeps no join to another table nothing else reads, which would only multiply them
    (SQLSearcher._simplified; Spider DEV 89, 91, 2026-10-14)."""
    found = set()
    for index in range(len(said)):
        if said[index:index + 2] == ("how", "many"):
            start = index + 2
        elif said[index] in {"number", "count"} and said[index + 1:index + 2] == ("of",):
            start = index + 2
        else:
            continue
        while start < len(said) and said[start] in {"the", "a", "an", "different", "distinct", "unique", "all"}:
            start += 1
        for table in schema.tables:
            name = tuple(canon(word) for word in name_words(table))
            if name and tuple(said[start:start + len(name)]) == name:
                # A table's name right after it is the noun's head, which counts: "How many movie ratings have more than
                # 3 stars?" counts the ratings (held-out Spider train 989, 2026-10-17: it counted the movies). Not a verb
                # taking an object: "How many instructors teach a course" counts instructors (fresh holdout 648).
                end = start + len(name)
                heads = [other for other in schema.tables
                         if (words := tuple(canon(word) for word in name_words(other)))
                         and tuple(said[end:end + len(words)]) == words
                         and said[end + len(words):end + len(words) + 1] not in _OBJECT_OPENERS]
                found.update(heads or (table,))
    return frozenset(found)


def counted_field_values(said: tuple[str, ...], schema: Any, start: int) -> tuple[ColumnRef, ...]:
    """The text fields the count noun at ``start`` names whole where it names no table: "How many continents speak
    Chinese?" counts the values of country.Continent, once each (Spider DEV 751, 2026-10-17: the count counted the
    countries). A noun naming a table counts its rows; "how many times" counts no field."""
    while start < len(said) and said[start] in {"the", "a", "an", "all", "of"}:
        start += 1
    if start >= len(said) or said[start] in {"time", "row", "record", "entry", "item"}:
        return ()
    noun = said[start]
    if any(noun in {canon(word) for word in name_words(table)} for table in schema.tables):
        return ()
    return tuple(sorted((schema_column.ref for schema_column in schema.columns
                         if schema_column.ref.type == SQLType.TEXT and not is_surrogate_key(schema_column.ref.name)
                         and tuple(canon(word) for word in name_words(schema_column.ref.name)) == (noun,)),
                        key=lambda column: (column.table, column.name))[:2])


def count_noun_starts(question: str, schema: Any) -> tuple[int, ...]:
    """Where each count's noun starts: after "how many", or after a "number of" or "count of" that is no field's
    number (``field_numbers``)."""
    said = tokens(question)
    numbers = field_numbers(question, (column.ref for column in schema.columns))
    return tuple(index + 2 for index in range(len(said))
                 if said[index:index + 2] == ("how", "many")
                 or (said[index] in {"number", "count"} and said[index + 1:index + 2] == ("of",)
                     and index not in numbers))


def counted_references(query: Any, question: str, schema: Any) -> tuple[tuple[Aggregate, str], ...]:
    """The counts in ``query`` that count the rows of a table referring to what the question counts, each with that
    table: "How many car models are produced in the usa?" counted car_names.Model, and then every row of car_names,
    once per car name of each model, where model_list holds the models (Spider DEV 115, 147, 148, 2026-10-17). The
    count's noun ("how many car models") says the referring key's own word and not its table's name; "How many flights
    does airline JetBlue have?" counts the flights holding the airline key, and "the phone number of these patients"
    counts nothing (field_numbers). A distinct count counts each row once."""
    if not isinstance(query, SelectQuery) or not isinstance(query.from_table, str):
        return ()
    said = tokens(question)
    counts = []
    having = [query.having] if query.having is not None else []
    while having:
        term = having.pop()
        having.extend(getattr(term, "terms", ()))
        counts.extend(side for side in (getattr(term, "left", None), getattr(term, "right", None))
                      if isinstance(side, Aggregate))
    counts.extend(expression for expression in (*(item.expression for item in query.select),
                                                *(term.expression for term in query.order_by))
                  if isinstance(expression, Aggregate))
    counts = [aggregate for aggregate in dict.fromkeys(counts) if aggregate.function == "COUNT" and not aggregate.distinct]
    if not counts:
        return ()
    read = {query.from_table, *(join.table for join in query.joins)}
    nouns = []
    for start in count_noun_starts(question, schema):
        while start < len(said) and said[start] in {"the", "a", "an", "all"}:
            start += 1
        nouns.append(set(said[start:start + 3]))
    # So does the noun a threshold on a number of rows counts: "makers who designed more than 3 car models" (Spider DEV
    # 149, 150, 2026-10-17: each kept the makers with more than 3 car names).
    nouns.extend(set(said[threshold.end:threshold.end + 3]) for threshold in count_thresholds(said))
    found = []
    for key in getattr(schema, "foreign_keys", ()):
        for child, _ in key.column_pairs:
            if child.table not in read or not schema.repeats((child,)):
                continue
            name = [canon(word) for word in name_words(child.name)]
            owner = {canon(word) for word in name_words(child.table)}
            if name and any(name[-1] in noun and not owner <= noun for noun in nouns):
                found.extend((aggregate, child.table) for aggregate in counts)
    return tuple(dict.fromkeys(found))


def scalar_counted_table(query: Any, said: Sequence[str], schema: Any) -> str | None:
    """The single named population of a simple scalar row count, shared by construction and checking.

    Grouped, ranked, aliased, derived and distinct-count readings retain their own scope policies.
    This binds COUNT(*) only; a count of a nullable field or stored quantity is a different reading.
    """
    if (not isinstance(query, SelectQuery) or not isinstance(query.from_table, str) or query.from_alias
            or any(join.alias for join in query.joins) or len(query.select) != 1
            or query.group_by or query.having is not None or query.order_by or query.limit is not None
            or set(said) & {"different", "distinct", "unique"} or not top_level_count(said)):
        return None
    expression = query.select[0].expression
    if not isinstance(expression, Aggregate) or expression.function != "COUNT" or not isinstance(expression.operand, Star):
        return None
    tables = counted_noun_tables(said, schema)
    return next(iter(tables)) if len(tables) == 1 else None


def count_preserves_population(query: SelectQuery, population: str, schema: Any) -> bool:
    """Each joined table matches at most one row per input row, at the named root's grain."""
    if query.from_table != population:
        return False
    return all(schema.identifies(tuple(column for left, right in join.predicates for column in (left, right)
                                      if column.table == join.table)) for join in query.joins)


def realizes_request(column: ColumnRef, said: Sequence[str], request: OutputRequest, schema: Any,
                     numbers: frozenset[int] = frozenset()) -> int:
    """How showing ``column`` realizes the field ``request`` asks for: 3 when the field's own name is the request's
    words, 2 when it says every word the request says among others, 1 when its table's name or, for the field that
    names its table's rows, "name" says the rest, 0 when
    it does not (at least one word must be the field's own). "Continent id" is ContId, not a Continent field nor
    another table's Id; "maker full name" a maker's FullName; "the role" role_code (Spider DEV 89, 109, 149,
    2026-10-14); "the name" of patients their Name before an appointment's display field (held-out Spider train 47).
    A "number" naming a field (``numbers``, field_numbers) asks for the word before it: "phone number" is Phone."""
    asked = {said[position] for position in range(request.start, request.end)
             if position not in numbers and said[position] not in FUNCTION_WORDS | _LIST_LEADS}
    own = set(name_part_words(column.name) or name_tokens(column.name))
    if abbreviated_table(column.name, column.table):
        own = (own - {name_tokens(column.name)[0]}) | set(name_tokens(column.table))
    displays = schema.display_columns(column.table) if hasattr(schema, "display_columns") else ()
    display = {"name", "title"} if displays and displays[0] == column else set()
    if not asked or not asked & (own | display):
        return 0
    entities = entity_words(schema) if hasattr(schema, "tables") else frozenset()

    def says(vocabulary: set[str]) -> bool:
        return all(word in vocabulary or any(says_in_another_form(word, known, entities) for known in vocabulary)
                   for word in asked)

    if says(own):
        # A field whose own name is the item's words, no more, is it before one whose name holds them among others:
        # "the names and areas of countries" is Name, not LocalName (Spider DEV 800, 2026-10-14).
        return 3 if all(word in asked or any(says_in_another_form(word, said, entities) for said in asked)
                        for word in own) else 2
    # An item whose last word stands where its field's name says only the kind of its values, the item saying the
    # rest of the name, is that field: "the cell phone" is cell_number (Spider DEV 994, 995, 2026-10-15).
    head = said[request.end - 1]
    rest = own - KIND_WORDS
    if rest and own & KIND_WORDS and head in asked - own and asked - {head} == rest:
        return 1
    return 1 if says(own | display | set(name_tokens(column.table))) else 0


def request_fields(question: str, schema: Any) -> tuple[tuple[ColumnRef, ...], ...]:
    """For each field an explicit list instruction asks for (output_requests), in order, the fields that realize it. The
    items of one list are one owner's unless an item names another: the table realizing the most of them, where one
    does, realizes each it can ("List full name and the id" of makers is the makers' FullName and Id, not a model's
    ModelId; Spider DEV 149, 2026-10-14). Empty where any requested field has none. The search builds them, and the
    ranker reads whether an answer shows them, from this one reading."""
    return _bound_fields(question, schema, output_requests(question, schema))


def _bound_fields(question: str, schema: Any, listed: Sequence[OutputRequest]) -> tuple[tuple[ColumnRef, ...], ...]:
    """request_fields over the requests ``listed``: output_requests reads them before it has returned them."""
    said = tokens(question)
    numbers = field_numbers(question, (column.ref for column in schema.columns))
    requests = [request for request in listed if not request.count]
    # A foreign key realizing a request gives way to the key it references, which names the same thing: "the club id"
    # of a count of coaches per club is the club's Club_ID, not also the coach's (fresh holdout 119, 2026-10-14).
    references = {key.from_column: key.to_column for key in getattr(schema, "foreign_keys", ())}
    # Each item's candidates are the fields whose own name says it, or, where none does, those its table's name or
    # display field says. The owner is decided over all items before an item prefers the field named exactly by it:
    # "Show the name and theme for all concerts" is the concert's concert_Name, not a singer's Name (Spider DEV 33).
    # The foreign keys give way only in deciding the owner: within it, its own key is its field ("the template type
    # codes" of templates is Templates.Template_Type_Code; Spider DEV 319, 327, 2026-10-14).
    candidates = []
    deciding = []
    every = []
    for request in requests:
        realized = [(level, column.ref) for column in schema.columns
                    if (level := realizes_request(column.ref, said, request, schema, numbers))]
        floor = 2 if any(level >= 2 for level, _ in realized) else 1
        options = [(level, ref) for level, ref in realized if level >= floor]
        refs = {ref for _, ref in options}
        candidates.append(options)
        every.append(realized)
        deciding.append([(level, ref) for level, ref in options if references.get(ref) not in refs])
    if not candidates or not all(every):
        return ()
    # A possessive opening the items names their owner, the one table named before them, where its fields realize
    # every item: "For each continent, list its id, name, and how many countries it has?" is the continent's ContId
    # and Continent, not a country's CountryId and CountryName (Spider DEV 90, 2026-10-15).
    before = said[:listed[0].start]
    antecedent = [table for table in schema.tables
                  if _says_in_order(before, [canon(word) for word in name_words(table)])]
    possessive = said[listed[0].start - 1:listed[0].start] in (("its",), ("their",), ("his",), ("her",))
    if possessive and len(antecedent) == 1:
        owned = [[(level, ref) for level, ref in realized if ref.table == antecedent[0]] for realized in every]
        if all(owned):
            candidates = deciding = owned
    if not all(candidates):
        return ()
    coverage = Counter(table for options in deciding for table in {ref.table for _, ref in options})
    # The table the list names right after its items owns them where the items alone tie: "the names and descriptions
    # of all the sections" are the sections' (Spider DEV 523, 524, 2026-10-14).
    after = [word for word in said[listed[-1].end:listed[-1].end + 6] if word not in FUNCTION_WORDS | _LIST_LEADS]
    for table in schema.tables:
        name = [canon(word) for word in name_words(table)]
        if name and coverage.get(table) and after[:len(name)] == name:
            coverage[table] += 0.5
    ranked = sorted(coverage.items(), key=lambda item: (-item[1], item[0]))
    owner = ranked[0][0] if len(ranked) == 1 or ranked[0][1] > ranked[1][1] else None
    if owner is None:
        # Of tables realizing the items alike, the one the question names before the list owns them: "Which head's
        # name has the substring 'Ha'? List the id and name." is the head's (held-out Spider train 903), while "the
        # ids and names of the web accelerators that are compatible with two or more browsers" names the browsers only
        # in its condition (fresh holdout 654). Where it names none, the items read no list: "the first name and last
        # name for all the instructors" over students and faculty (held-out Spider train 529, 2026-10-14).
        tied = [table for table, count in ranked if count == ranked[0][1]]
        named = [table for table in tied if table in antecedent]
        if len(named) != 1:
            return ()
        owner = named[0]
    # An item followed by "of" and the rows of one table realizing it is that table's, whatever the list's owner:
    # "Find the customer name and date of the orders" is the orders' order_date, not the customers'
    # date_became_customer (fresh holdout 509, 2026-10-15).
    pinned: dict[int, str] = {}
    for index, request in enumerate(requests):
        if possessive or said[request.end:request.end + 1] != ("of",):
            continue
        rest = [word for word in said[request.end + 1:request.end + 5] if word not in _OWNER_LEADS]
        tables = {table for table in schema.tables
                  if (name := [canon(word) for word in name_words(table)])
                  and rest and (rest[:len(name)] == name or name[-1] == rest[0])}
        owners = {ref.table for _, ref in candidates[index] if ref.table in tables}
        if len(owners) == 1:
            pinned[index] = owners.pop()
    fields = []
    for index, (options, decided) in enumerate(zip(candidates, deciding)):
        inside = [(level, ref) for level, ref in options if ref.table == pinned.get(index, owner)] or decided
        top = max(level for level, _ in inside)
        best = [ref for level, ref in inside if level == top]
        # Of the owner's fields an item names alike, its own before one referring to another table: "the id" of
        # accounts is account_id, not customer_id (held-out Spider train 113, 2026-10-14). A foreign key and the key
        # it references hold the same values: either realizes the item ("the grape" of wines, held-out 828).
        best = [ref for ref in best if ref not in references] or best
        named = {ref for _, ref in options}
        fields.append(tuple(dict.fromkeys([*best, *(references[ref] for ref in best
                                                    if references.get(ref) in named)])))
    return tuple(fields)


# The words between "of" and the rows it names that name none of them.
_OWNER_LEADS = frozenset({"the", "a", "an", "each", "every", "all", "its", "their"})


def _says_in_order(said: Sequence[str], words: Sequence[str]) -> bool:
    """Whether ``said`` holds ``words`` together, in order."""
    return bool(words) and any(list(said[index:index + len(words)]) == list(words)
                               for index in range(len(said) - len(words) + 1))


def repeated_requests(question: str, schema: Any = None) -> tuple[int | None, ...]:
    """For each of ``question``'s output requests (output_requests), the earlier request it says again in the same words,
    asking to show that field once more ("List the id, name and id."), else None. A request another shown field could
    also realize is no repetition: "the contestant numbers and names" asks for two fields, and "the names of cities, as
    well as the names of the counties" for two names (Spider DEV 688, held-out Spider train 269, 2026-10-16: each was
    answered with one field shown twice, and the right answer refused as missing the repetition)."""
    said = tokens(question)
    first: dict[tuple[str, ...], int] = {}
    out: list[int | None] = []
    for index, request in enumerate(output_requests(question, schema)):
        words = said[request.start:request.end]
        out.append(None if request.count else first.get(words))
        if not request.count:
            first.setdefault(words, index)
    return tuple(out)


def shown_once(fields: Sequence[ColumnRef], question: str, schema: Any) -> tuple[ColumnRef, ...]:
    """``fields``, one for each field request of ``question`` in its order (request_fields), with a field an earlier
    request binds kept again only where its request repeats that one (repeated_requests)."""
    requests = [index for index, request in enumerate(output_requests(question, schema)) if not request.count]
    repeats = repeated_requests(question, schema)
    out: list[ColumnRef] = []
    for field, index in zip(fields, requests):
        if field not in out or repeats[index] is not None:
            out.append(field)
    return tuple(out)


def request_slots(question: str, schema: Any) -> tuple[frozenset[ColumnRef] | None, ...]:
    """The outputs an explicit list instruction asks for, in its order: for a field, the fields realizing it
    (request_fields); None for a count. Empty unless every requested field has one."""
    fields = list(request_fields(question, schema))
    requests = output_requests(question, schema)
    if not fields and any(not request.count for request in requests):
        return ()
    return tuple(None if request.count else frozenset(fields.pop(0)) for request in requests)


def _output_request(said: tuple[str, ...], start: int, end: int,
                    numbers: frozenset[int] = frozenset()) -> OutputRequest | None:
    # "List his or her role, street, city and state." asks for the role (Spider DEV 970, 2026-10-15).
    while start < end and (said[start] in _LIST_LEADS | {"and", "as", "well", "along", "together", "with"}
                           or (said[start] == "or" and start > 0 and said[start - 1] in {"his", "her"})):
        start += 1
    if start >= end:
        return None
    head = start + 1 if said[start] == "total" else start
    # "List the continent name and the count." asks for the count (Spider DEV 105, 2026-10-14).
    count = said[start:end] == ("count",) or said[start:start + 2] == ("how", "many") or (
        said[head:head + 1] in (("number",), ("count",)) and said[head + 1:head + 2] == ("of",) and head < end
        and head not in numbers)
    return OutputRequest(start, end, count)


def tokens(text: str) -> tuple[str, ...]:
    """The comparable words of ``text`` (``canon`` of each of its ``words``): the one tokenization the search, its
    expansions and the completeness gate read a question with."""
    return tuple(canon(token) for token in words(text))
