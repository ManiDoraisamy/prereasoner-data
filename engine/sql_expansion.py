"""Question-word helpers shared by the date and duration readers, the registered calculations, decomposition and
the world routes: tokens, number words, field names and the cue words they read.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Any, Iterable, Sequence
import weakref

from engine.sql_ast import (
    ColumnRef,
    SQLType,
)
from engine.closed_class import COMPARATIVES, QUANTITY_COMPARATIVES, spacy_model
from engine.numeric import NUMBER_TEXT, NUMBER_WORD, parse_decimal
from engine.sql_schema import SchemaGraph, canon, is_surrogate_key, name_words


# Superlative words, by the direction they rank in.
MAX_CUES = frozenset({
    "biggest", "commonest", "greatest", "heaviest", "highest", "largest", "latest",
    "longest", "max", "maximum", "most", "newest", "oldest", "tallest", "youngest",
})
MIN_CUES = frozenset({
    "cheapest", "earliest", "fewest", "least", "lightest", "lowest", "min", "minimum", "rarest",
    "shortest", "smallest",
})

WORD_NUMBERS = {
    "zero": 0, "one": 1, "single": 1, "two": 2, "couple": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10,
}


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


def parse_number(token: str):
    if token in WORD_NUMBERS:
        return WORD_NUMBERS[token]
    if NUMBER_TEXT.match(token):
        token = token.replace(",", "")
        return parse_decimal(token) if "." in token else int(token)
    return None


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
# Words that end a noun phrase: grammar words, and the words that open a clause, a comparison, a grouping or an
# ordering. Any other word after a column's name continues the phrase that name begins. "column" and "field" end
# it too: "the Name column" is the Name.
PHRASE_BREAKS = frozenset(canon(word) for word in FUNCTION_WORDS | {
    "all", "any", "each", "every", "per", "group", "these", "those", "some", "no", "not", "nor", "but",
    "who", "whom", "whose", "which", "where", "when", "why", "how", "what", "there",
    "has", "have", "had", "do", "does", "did", "can", "could", "will", "would", "should", "shall", "may", "might",
    "having", "ordered", "sort", "sorted", "top", "bottom", "after", "before", "between", "during",
    "since", "until", "within", "without", "across", "over", "under", "above", "below", "among", "through",
    "versus", "vs", "more", "less", "fewer", "greater", "than", "excluding", "except", "including",
    "containing", "use", "using", "used", "column", "field",
})
# Words that name the kind of a column's values rather than what they are: "the emails" are email_address,
# "the role" role_code, "the cell phone" cell_number (canon() forms). "name" is not one: "the first student"
# names no first_name.
KIND_WORDS = frozenset({"code", "number", "address", "date", "description", "details", "value", "text",
                        "flag", "yn"})


def entity_words(schema: Any) -> frozenset[str]:
    """The words of the schema's table names (canon() forms), read once per schema."""
    entities = _ENTITY_WORDS.get(schema)
    if entities is None:
        entities = _ENTITY_WORDS[schema] = frozenset(word for table in schema.tables for word in name_tokens(table))
    return entities


_ENTITY_WORDS: "weakref.WeakKeyDictionary[Any, frozenset[str]]" = weakref.WeakKeyDictionary()


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


def names_operation(word: str) -> bool:
    """Whether ``word`` shares a stem with an operation a query performs (``OPERATION_STEMS``): "ordered", "listed",
    "ranked" say what the query does, not what its rows are."""
    return any(stem.startswith(operation) or operation.startswith(stem)
               for stem in word_stems(word) for operation in OPERATION_STEMS)


def says_in_another_form(word: str, name_word: str, entities: frozenset[str] = frozenset()) -> bool:
    """Whether the question's ``word`` says ``name_word``, a word of a table's or field's name, in another form: they
    share a stem of five letters or more ("enrolled" Student_Enrolment, "arriving" date_arrived, "directors"
    directed_by), or one is the other's abbreviation (``spells_out``: "account balance" acc_bal, "latitude" lat,
    "institution" Inst) and the word spelling it out names no table (``entity_words``: "students" spells out no
    ENROLL.STU_NUM, the abbreviation of a table's name being read beside its table, ``abbreviated_table``; held-out
    Spider train 58, 557, 938, 2026-10-16: three right answers showed it), and the word names no operation ("ordered"
    by an Orders table). Both are canon() forms."""
    if not word.isalpha() or names_operation(word):
        return False
    stems = word_stems(word)
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
    column_matches reads it."""
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


def money_total_position(question_tokens: Sequence[str], table_words: Iterable[str], count_asked: bool) -> int | None:
    """Position of a money noun that names a table and asks for its money total, else None.

    Tokens and table words are canon() forms; ``count_asked`` is the question's ``count_requested``. The world-join
    operand choice (encoder_overlay.read_op_all) applies it, and still requires a money measure column in that table,
    without which the noun keeps its entity reading.
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


def graph_measures(graph: Any) -> list[dict]:
    """A schema graph's columns as the planner-schema entries the measure readers take ({table, name, affinity})."""
    return [{"table": column.ref.table, "name": column.ref.name,
             "affinity": "REAL" if column.ref.type.numeric else "TEXT"} for column in graph.columns]


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
    whole name, or the people a population counts (``counted_population``: "how many people" totals
    Population), right after "how many", "number of" or "count of".
    """
    question_tokens = tokens(question)
    starts = [index + 2 for index in range(len(question_tokens) - 1)
              if question_tokens[index:index + 2] in (("how", "many"), ("number", "of"), ("count", "of"))]
    # "How many total tours" counts the tours as "how many tours" does (Spider DEV 480, 2026-10-18).
    starts += [start + 1 for start in starts if question_tokens[start:start + 1] == ("total",)]
    return [entry for entry in _numeric_measures(sch)
            if (words := name_tokens(entry["name"]))
            and any(question_tokens[start:start + len(words)] == words
                    or counted_population(entry["name"], question_tokens[start:start + 1])
                    for start in starts)]


# The words for the people a population counts (``counted_population``).
POPULATION_NOUNS = frozenset({"person"})                  # canon's form of "people"


def counted_population(name: str, said: Sequence[str]) -> bool:
    """Whether the words ``said`` are the people the field ``name`` counts as a population: "How many people live in
    Gelderland district?" totals Population, and "the 3 countries with the fewest people" ranks by it (Spider DEV 728,
    767, 805, 2026-10-17: no reading read the people as it). Only as a quantity counted or graded: "the largest
    percentage of people" names no Population (Spider DEV 816, 820)."""
    return len(said) == 1 and said[0] in POPULATION_NOUNS and name_tokens(name) == ("population",)


def compatible_types(left: SQLType, right: SQLType) -> bool:
    return left == right or (left.numeric and right.numeric)


def name_tokens(name: str) -> tuple[str, ...]:
    return tuple(canon(word) for word in name_words(name))


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


@dataclass(frozen=True)
class CueNames:
    """The places a word that asks a superlative or an aggregate is also a numeric field's whole one-word name
    ("Highest", "Average", "Total"), read by what follows it (``cue_names``).

    ``grading``: the word grades the field named right after it, and names no field of its own: "the stadium with the
    highest capacity" ranks Capacity, "the highest average attendance" ranks Average (Spider DEV 18, 19, 43,
    2026-10-17: Highest was shown and ranked, and the count asked was lost). ``measured``: the word is the field, the
    noun after it what the field measures, where a superlative or an ordering grades it: "the highest total score",
    "in descending order of total scores" rank Total and total nothing (held-out Spider train 664, 803). Before a
    noun no superlative or ordering grades ("the total sales in Paris"), the word still asks its aggregate."""
    grading: frozenset[int]
    measured: frozenset[int]


_ORDER_LEADS = frozenset({"order", "ordered", "sorted", "sort"})


def cue_names(question_tokens: Sequence[str], schema: Any) -> CueNames:
    """Where the question's superlative and aggregate words are numeric fields' names (``CueNames``)."""
    names = {}
    for column in schema.columns:
        words = [canon(word) for word in name_words(column.ref.name)]
        for word in words:
            names.setdefault(word, set()).add(column.ref)
    single = {words[0] for column in schema.columns if column.ref.type.numeric
              and len(words := [canon(word) for word in name_words(column.ref.name)]) == 1}
    table_words = {canon(word) for table in schema.tables for word in name_words(table)}
    cues = (MAX_CUES | MIN_CUES | set(AGGREGATE_CUES)) & single
    grading, measured = set(), set()
    for index, token in enumerate(question_tokens[:-1]):
        if token not in cues:
            continue
        after = question_tokens[index + 1]
        others = {ref for ref in names.get(after, ()) if [canon(word) for word in name_words(ref.name)] != [token]}
        if others:
            grading.add(index)
            continue
        before = set(question_tokens[max(0, index - 2):index])
        graded = bool(before & (MAX_CUES | MIN_CUES)) or (
            index >= 2 and question_tokens[index - 1] in {"of", "by"} and question_tokens[index - 2] in _ORDER_LEADS)
        if (token in AGGREGATE_CUES and graded and after.isalpha() and after not in names and after not in table_words
                and after not in PHRASE_BREAKS):
            measured.add(index)
    return CueNames(frozenset(grading), frozenset(measured))


# The words before "the" that make an extreme select the row holding it (``selects_extreme``).
_SELECTOR_LEADS = frozenset({"with", "has", "have", "had", "having"})
_EXTREME_WORDS = {"maximum": "MAX", "max": "MAX", "minimum": "MIN", "min": "MIN"}


def selects_extreme(question_tokens: Sequence[str], position: int) -> bool:
    """Whether the extreme at ``position`` ("maximum", "minimum") selects the row holding it, as "the highest" does,
    rather than asking for the extreme itself: "the country with the maximum GNP", "Which model of the car has the
    minimum horsepower?" (Spider DEV 95, 2026-10-15: 11 such questions, none answered right)."""
    return (question_tokens[position] in _EXTREME_WORDS and position >= 2 and question_tokens[position - 1] == "the"
            and question_tokens[position - 2] in _SELECTOR_LEADS)


def asked_cues(cues: list[tuple[str, int]], question_tokens: tuple[str, ...],
               schema: Any) -> list[tuple[str, int]]:
    """The aggregate cues (function, position) that ask for their aggregate, as the search and the ranker read
    them. A cue inside a several-word column name the question spells (``spelled_names``) names that column: it
    asks only when no other cue does, and an average never does, since an average of rows' averages is not
    their average: "the Avg. monthly searches for all Keyword containing 'inspection checklist'" lists the 38
    keywords' searches rather than averaging them (a customer's keyword sheet, 2026-10-06). Nor does a count: "in
    ascending order of room count" orders by room_count and counts nothing (fresh holdout of Spider train 180,
    2026-10-17). The total, maximum or minimum of rows' totals, maxima or minima is theirs: "the total amount in
    Paris" sums Total Amount. An extreme that selects the row holding it asks for no aggregate (``selects_extreme``), nor
    does a word that is the field a superlative or an ordering grades ("the highest total score", ``cue_names``)."""
    measured = cue_names(question_tokens, schema).measured
    cues = [(function, position) for function, position in cues
            if not selects_extreme(question_tokens, position) and position not in measured]
    # "How many total tours" asks one number: the total of the numeric field the noun after "total" names whole, the
    # count it holds, else the count of what the noun names (Spider DEV 480, 2026-10-18: a count of tours was shown
    # beside their total).
    for index in range(len(question_tokens) - 3):
        if tuple(question_tokens[index:index + 3]) == ("how", "many", "total"):
            stored = any(question_tokens[index + 3:index + 3 + len(words)] == words
                         for entry in counted_measure_columns(" ".join(question_tokens), graph_measures(schema))
                         if (words := name_tokens(entry["name"])))
            cues = [(function, position) for function, position in cues
                    if not ((function == "COUNT" and position in (index, index + 1)) if stored
                            else (function == "SUM" and position == index + 2))]
    spelled = spelled_names(question_tokens, schema)
    if any(position not in spelled for _, position in cues):
        return [(function, position) for function, position in cues if position not in spelled]
    return [(function, position) for function, position in cues if function not in {"AVG", "COUNT"}]


# A "%" standing alone is the word "percent" ("what % of the total amount comes from Paris" served the
# Paris total, 2026-10-02); after a number ("over 50%") it is the number's unit, and inside a word or
# quotes ("names like 'A%'") a pattern's wildcard.
_QUESTION_WORD = re.compile(NUMBER_WORD + r"|[^\W_]+(?:'[^\W_]+)?|(?<![\w%'\"])(?<!\d\s)%(?![\w'\"])", re.UNICODE)


# Typographic apostrophes and quotes, and the replacement character a mis-decoded one leaves, as the ASCII marks the
# readers of a question parse (``plain_marks``).
_TYPOGRAPHIC_MARKS = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"',
                                    "�": "'"})
# A comparison symbol before a number, as the words the readers read it by.
_COMPARISON_SYMBOL = re.compile(r"(>=|<=|!=|<>|>|<)(?=\s*-?\d)")
_SYMBOL_WORDS = {">=": " at least ", "<=": " at most ", "!=": " other than ", "<>": " other than ", ">": " above ",
                 "<": " below "}


# "Prior to" is "before", the word every comparison and date reader reads: "exhibitions that happened prior to 2009"
# compared Year = 2009 (fresh holdout of Spider train 273, 2026-10-17).
_PRIOR_TO = re.compile(r"(?i)(?<![\w'])prior\s+to\b")


def plain_marks(question: str) -> str:
    """``question`` with its typographic apostrophes and quotes written as the ASCII ones, and its comparison symbols
    as words: "Brazil’s population" said "Brazil" and a lone "s", and “Smith” quoted nothing (Spider DEV 715, 719,
    held-out 202, 2026-10-16: each was refused); "instructors with salary > 80000" compared salary = 80000, the symbol
    being no word (fresh holdout of Spider train 103, 2026-10-17). Selection reads a question so
    (engine/tables.py:TableQuery.select_query)."""
    text = str(question).translate(_TYPOGRAPHIC_MARKS).replace("``", '"').replace("''", '"')
    text = _PRIOR_TO.sub("before", text)
    return _COMPARISON_SYMBOL.sub(lambda match: _SYMBOL_WORDS[match.group(1)], text)


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
    stand for the whole answer. Only a closed list may remove an output no item asks for. Past a word opening the
    condition the rows meet, "and" joins conditions: "Find the districts in which there are both shops selling less
    than 3000 products and shops selling more than 10000 products." asks for the districts alone (Spider DEV 294,
    2026-10-18: refused), "List the document ids of documents with the status done and type Paper" for the ids."""
    if not requests:
        return False
    said, spans = tokens(question), word_spans(question)
    if len(said) != len(spans):
        return False
    for index in range(requests[-1].end, len(said)):
        if index and re.search(r"[.!?;]", question[spans[index - 1][1]:spans[index][0]]):
            break
        if said[index] in _CONDITION_LEADS:
            return True
        if said[index] in {"and", "or", "also", "together", "plus"}:
            return False
    return True


# The words opening the condition the rows a list reads meet ("documents with the status done").
_CONDITION_LEADS = frozenset({"with", "where", "whose", "who", "that", "which", "when", "if"})


def _joins_items(said: Sequence[str], position: int) -> int:
    """The length of the words at ``position`` that add an item to a list ("along with", "together with", "as well
    as", and "and" before a possessive: "the name and date for each race and its track name"), else 0."""
    if tuple(said[position:position + 2]) in (("along", "with"), ("together", "with")):
        return 2
    if said[position:position + 1] == ("and",) and said[position + 1:position + 2] in (
            ("its",), ("their",), ("his",), ("her",)):
        return 1
    return 3 if tuple(said[position:position + 3]) == ("as", "well", "as") else 0


# The words that open a verb's object: a word before one is no noun's head.
_OBJECT_OPENERS = frozenset({("a",), ("an",), ("the",), ("any",), ("some",), ("all",), ("every",), ("each",), ("their",),
                             ("its",), ("his",), ("her",), ("more",), ("at",)})


def counted_noun_tables(said: Sequence[str], schema: Any) -> frozenset[str]:
    """The tables whose rows a count counts: those the noun right after "how many", "number of" or "count of" names
    ("How many countries does each continent have?" counts the countries)."""
    found = set()
    for index in range(len(said)):
        if said[index:index + 2] == ("how", "many"):
            start = index + 2
        elif said[index] in {"number", "count"} and said[index + 1:index + 2] == ("of",):
            start = index + 2
        # So do "most" and "fewest" before a table's name: "the year that had the most concerts" counts the concerts
        # (Spider DEV 27, 2026-10-17: a count of each concert's singers ranked the years).
        elif said[index] in {"most", "fewest"}:
            start = index + 1
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
    # The table the list names right after its items owns them where the items alone tie, by its name or the noun that
    # ends it: "the names and descriptions of all the sections" are the sections' (Spider DEV 523, 524, 2026-10-14),
    # "the names and ids of all makers" car_makers' (Spider DEV 150, 2026-10-18: tied with the countries' and read no
    # list, so a maker's name alone was shown).
    after = [word for word in said[listed[-1].end:listed[-1].end + 6] if word not in FUNCTION_WORDS | _LIST_LEADS]
    for table in schema.tables:
        name = [canon(word) for word in name_words(table)]
        if name and coverage.get(table) and (after[:len(name)] == name or after[:1] == name[-1:]):
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
