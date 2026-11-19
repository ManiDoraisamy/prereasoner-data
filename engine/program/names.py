"""The named coordinates of a program: every closed list the language, the decoder and the verifier read.

A form, an aggregate, a direction, a comparison, an answer type, a word role, a condition kind or a slot is one of these strings and nothing else. The later reader fills exactly these names, so a name added here
is a coordinate added to the engine. A score reaches the decoder only through a coordinate (see coordinate()), and a contribution names one.
"""
from __future__ import annotations

# The four forms.
SHOW = "show"          # a labelled cell, or a list of the values on the kept lines
AGG = "agg"            # sum, average, lowest, highest, count or distinct count over the kept lines
RANK = "rank"          # the top line, group or peer column
CALC = "calc"          # the difference, sum or ratio of two programs that each give one number
FORMS = (SHOW, AGG, RANK, CALC)
# Two forms the small reader has no head for (its closed form head is FORMS): CLEF's reading and the decoder name them.
CHECK = "check"        # a yes or no: whether any, no or every kept line meets a condition, or how one number compares
BREAKDOWN = "breakdown"  # a figure (sum, average, lowest, highest or count) for each group of a key column: one row each
PROGRAM_FORMS = (*FORMS, CHECK, BREAKDOWN)

# What an aggregate does.
SUM = "sum"
AVG = "avg"
MIN = "min"
MAX = "max"
COUNT = "count"
COUNT_DISTINCT = "countd"
AGGREGATES = (SUM, AVG, MIN, MAX, COUNT, COUNT_DISTINCT)
NUMBER_AGGREGATES = (SUM, AVG, MIN, MAX)

# Which way a rank runs.
HIGHEST = "highest"
LOWEST = "lowest"
DIRECTIONS = (HIGHEST, LOWEST)

# Whether a figure is read with its sign or by size.
SIGNED = "signed"
BY_SIZE = "by_size"        # a group or a peer column ranks by the size of its total
EACH_BY_SIZE = "each"      # a measure line by line, its sign dropped
STATED_BY_SIZE = "stated"  # the signed result, stated by its size
SIZES = (SIGNED, BY_SIZE, EACH_BY_SIZE, STATED_BY_SIZE)

# What a rank ranks.
ENTITY_LINE = "line"       # the line holding the top value
ENTITY_GROUP = "group"     # the key whose lines add up highest
ENTITY_PEER = "peer"       # the peer column holding the top value across the kept lines
RANK_ENTITIES = (ENTITY_LINE, ENTITY_GROUP, ENTITY_PEER)

# How a number compares.
GE = ">="
GT = ">"
LE = "<="
LT = "<"
NE = "!="
COMPARISONS = (GE, GT, LE, LT, NE)
EQ = "="
CHECK_OPS = (GE, GT, LE, LT, EQ, NE)
# What a number condition may do: the small reader's closed head reads COMPARISONS; equality ("with zero calories",
# "exactly 3 wins") is the plain reading of a stated number no comparison word qualifies.
COMPARE_OPS = (EQ, *COMPARISONS)

# What a check asks of the kept lines.
ANY = "any"            # some kept line exists
NO = "no"              # no kept line exists
EVERY = "every"        # every line the base conditions keep also meets one more condition
VERSUS = "versus"      # one number against a stated number or another program's number
IS = "is"              # the top line's shown value, or the top group's key, against a stated text or number
SAME = "same"          # the top line (or group) by one rank is also the top one by another
QUANTIFIERS = (ANY, NO, EVERY, VERSUS, IS, SAME)

# Which line a read takes beside the one its conditions keep ("who finished after Bo?"): a slot the small reader has
# no head for; CLEF's reading names it.
AFTER = "after"        # the first data line below the first kept line
BEFORE = "before"      # the last data line above it
NEIGHBOURS = (AFTER, BEFORE)

# Arithmetic on one line, and between two programs.
PLUS = "+"
MINUS = "-"
TIMES = "*"
OVER = "/"
ROW_OPS = (PLUS, MINUS, TIMES, OVER)
PERCENT_OP = "%"   # a rate's percent of a measure (PercentOf): a row operator apart from ROW_OPS, so the trained readers' operator
                   # classes stay ROW_OPS and only a reading that scores it offers a percent
GRAIN_MONTH = "year_month"     # a breakdown's date key grouped by its year-month (sql_ast.DatePart's part of that name)
CALC_OPS = (MINUS, PLUS, OVER)

# What the answer is. The fine list is what the question reads as (answer_location.txt); a program states a coarse type, since a sheet does not say whether a name is a person's or a company's.
A_NUMBER = "number"
A_COUNT = "count"
A_PERCENT = "percent"
A_DATE = "date"
A_PERSON = "person"
A_COMPANY = "company"
A_CODE = "code"
A_LIST = "list"
A_NAME = "name"
A_PLACE = "place"
A_MONEY = "money"
A_QUANTITY = "quantity"
A_YES_NO = "yes_no"
ANSWER_TYPES = (A_NUMBER, A_COUNT, A_PERCENT, A_DATE, A_PERSON, A_COMPANY, A_CODE, A_LIST, A_PLACE, A_MONEY, A_QUANTITY)
PROGRAM_ANSWER_TYPES = (A_NUMBER, A_COUNT, A_PERCENT, A_DATE, A_NAME, A_LIST, A_YES_NO)
NAME_TYPES = (A_PERSON, A_COMPANY, A_CODE, A_PLACE)
FIGURE_TYPES = (A_MONEY, A_QUANTITY)

# What a word of the question does.
ROLE_MEASURE = "measure"
ROLE_KEY = "key"
ROLE_FILTER = "filter"
ROLE_CONTEXT = "context"
WORD_ROLES = (ROLE_MEASURE, ROLE_KEY, ROLE_FILTER, ROLE_CONTEXT)

# The conditions.
C_EQUALS = "equals"
C_CONTAINS = "contains"
C_PERIOD = "period"
C_COMPARISON = "comparison"
C_BLANK = "blank"
C_SECTION = "section"
C_LINE = "line"
C_NOT_EQUALS = "not_equals"
CONDITION_KINDS = (C_EQUALS, C_CONTAINS, C_PERIOD, C_COMPARISON, C_BLANK, C_SECTION, C_LINE, C_NOT_EQUALS)

# Where a contained text must sit in its cell.
ANYWHERE = "anywhere"
STARTS = "starts"
ENDS = "ends"
ANCHORS = (ANYWHERE, STARTS, ENDS)

# What a measure is.
M_COLUMN = "column"
M_ROW_ARITHMETIC = "row_arithmetic"
M_SIZE = "size"
M_DATE = "date"
M_ACROSS = "across"
MEASURE_KINDS = (M_COLUMN, M_ROW_ARITHMETIC, M_SIZE, M_DATE, M_ACROSS)

# The slots a program's coordinates fill. Each has one weight in the additive formula.
SLOT_FORM = "form"
SLOT_AGGREGATE = "aggregate"
SLOT_DIRECTION = "direction"
SLOT_SIZE = "size"
SLOT_ENTITY = "entity"
SLOT_COMPARISON = "comparison"
SLOT_ROW_OP = "row_op"
SLOT_CALC_OP = "calc_op"
SLOT_CALC_LEFT = "calc_left"
SLOT_ANSWER_TYPE = "answer_type"
SLOT_TABLE = "table"
SLOT_MEASURE = "measure"
SLOT_KEY = "key"
SLOT_SHOWN = "shown"
SLOT_FILTER = "filter"
SLOT_VALUE = "value"
SLOT_CONTAINS = "contains"
SLOT_LINE = "line"
SLOT_SECTION = "section"
SLOT_PERIOD = "period"
SLOT_NUMBER = "number"
SLOT_BLANK = "blank"
SLOT_PEERS = "peers"
SLOT_LISTING = "listing"
SLOT_CONDITIONS = "conditions"
SLOT_QUANTIFIER = "quantifier"
SLOT_NEIGHBOUR = "neighbour"
SLOTS = (SLOT_FORM, SLOT_AGGREGATE, SLOT_DIRECTION, SLOT_SIZE, SLOT_ENTITY, SLOT_COMPARISON, SLOT_ROW_OP, SLOT_CALC_OP, SLOT_CALC_LEFT, SLOT_ANSWER_TYPE, SLOT_TABLE, SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN, SLOT_FILTER, SLOT_VALUE,
         SLOT_CONTAINS, SLOT_LINE, SLOT_SECTION, SLOT_PERIOD, SLOT_NUMBER, SLOT_BLANK, SLOT_PEERS, SLOT_LISTING, SLOT_CONDITIONS, SLOT_QUANTIFIER, SLOT_NEIGHBOUR)

# The fixed semantics, each named so a read-as line or a test can cite one.
SEM_SIGNS = "signs"
SEM_BLANKS = "blanks"
SEM_PRINTED = "printed_lines"
SEM_TIES = "ties"
SEM_PERIODS = "periods"
SEM_UNITS = "units"
SEM_PARTIAL = "partial_value"
SEM_FOLD = "case_and_spacing"
SEM_MIXED = "mixed_columns"
SEM_REPEATED = "repeated_rows"
SEMANTICS = (SEM_SIGNS, SEM_BLANKS, SEM_PRINTED, SEM_TIES, SEM_PERIODS, SEM_UNITS, SEM_PARTIAL, SEM_FOLD, SEM_MIXED, SEM_REPEATED)

# What a question's sign word says: how a figure stored with a sign is read.

# What a question's period words say.

# The verifier's evidence. Each result carries these by name; none of them decides alone until the chooser weighs them.
EV_RAN = "ran"
EV_FAILED = "failed"
EV_EMPTY = "empty"
EV_BARE_ZERO = "bare_zero"
EV_ROW_DUMP = "row_dump"
EV_MANY_ROWS = "many_rows_for_one_value"
EV_WRONG_KIND = "wrong_kind"
EV_ROW_COUNT_FOR_AMOUNT = "row_count_for_amount"
EV_FRACTION_FOR_COUNT = "fraction_for_count"
EV_TOO_LARGE_FOR_SHARE = "too_large_for_share"
EV_TYPE_AGREES = "type_agrees"
EV_SHAPE_AGREES = "shape_agrees"
EV_PRINTED_TIE = "printed_total_tie"
EV_FILTER_DID_NOTHING = "filter_did_nothing"
EV_REPEATED_LINES = "first_of_repeated_lines"
EV_ADDS_TWICE = "adds_twice"
EVIDENCE = (EV_RAN, EV_FAILED, EV_EMPTY, EV_BARE_ZERO, EV_ROW_DUMP, EV_MANY_ROWS, EV_WRONG_KIND, EV_ROW_COUNT_FOR_AMOUNT, EV_FRACTION_FOR_COUNT, EV_TOO_LARGE_FOR_SHARE, EV_TYPE_AGREES, EV_SHAPE_AGREES, EV_PRINTED_TIE,
            EV_FILTER_DID_NOTHING, EV_REPEATED_LINES, EV_ADDS_TWICE)


# Which word role fills which slot: the question reader's word heads read onto the program's slots through this.
ROLE_OF_SLOT = {SLOT_MEASURE: ROLE_MEASURE, SLOT_KEY: ROLE_KEY, SLOT_SHOWN: ROLE_KEY, SLOT_PEERS: ROLE_KEY, SLOT_FILTER: ROLE_FILTER, SLOT_VALUE: ROLE_FILTER, SLOT_CONTAINS: ROLE_FILTER, SLOT_LINE: ROLE_FILTER, SLOT_SECTION: ROLE_FILTER, SLOT_PERIOD: ROLE_FILTER, SLOT_NUMBER: ROLE_FILTER,
                SLOT_BLANK: ROLE_FILTER, SLOT_TABLE: ROLE_CONTEXT}


def coarse_type(fine: str) -> str:
    """The type a program can state for what a question reads as: a person or a place is a name, a sum of money a number."""
    return A_NAME if fine in NAME_TYPES else A_NUMBER if fine in FIGURE_TYPES else fine


def coordinate(slot: str, *parts: object) -> str:
    """A coordinate's name: its slot, then what fills it. coordinate(SLOT_AGGREGATE, SUM) is "aggregate:sum"; coordinate(SLOT_VALUE, "t0.3", "ees") is "value:t0.3=ees"."""
    if slot not in SLOTS:
        raise ValueError(f"unknown slot: {slot}")
    return f"{slot}:" + "=".join(str(part) for part in parts)


def slot_of(name: str) -> str:
    """The slot a coordinate's name fills."""
    return name.split(":", 1)[0]
