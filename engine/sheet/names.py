"""The named coordinates of a compiled sheet: every closed list the sheet compiler writes and later layers read.

A line role, a column role, a value kind, a stray kind, a formula shape, a period kind or a line signal is one of these
strings and nothing else. No other module spells one of them as a literal; an additive decision downstream reads these
names, so a name added here is a coordinate added to the engine.
"""
from __future__ import annotations

# What a line of a tab is.
RECORD = "record"          # a data line
SUBTOTAL = "subtotal"      # a printed summary of part of a table: searchable by label, never added up
TOTAL = "total"            # a printed summary of a whole table: searchable by label, never added up
BAND = "band"              # a section label over the lines under it
HEADER = "header"          # names columns
TITLE = "title"            # context above a table
NOTE = "note"              # a label: value line, or text outside any table's records
BLANK = "blank"            # nothing on the line
LINE_ROLES = (RECORD, SUBTOTAL, TOTAL, BAND, HEADER, TITLE, NOTE, BLANK)
PRINTED_ROLES = (SUBTOTAL, TOTAL)

# What a column of a table is.
KEY = "key"                        # text naming the record, mostly distinct
LABEL = "label"                    # text with repeats: a category to filter or group by
SECTION_LABEL = "section_label"    # a sparse label column, filled down over the lines under each label
MEASURE = "measure"                # numbers
DATE = "date"                      # typed or written days
PERIOD = "period"                  # months, quarters, years written as words
PEER = "peer"                      # one of several columns of one axis: months across
ROW_TOTAL = "row_total"            # adds up its own line: a printed figure, never a peer of what it adds
DERIVED = "derived"                # arithmetic on its own line, computed by the sheet
TAB_NAME = "tab_name"              # the tab a family member came from
SPACER = "spacer"                  # empty inside a table's range
COLUMN_ROLES = (KEY, LABEL, SECTION_LABEL, MEASURE, DATE, PERIOD, PEER, ROW_TOTAL, DERIVED, TAB_NAME, SPACER)

# What a cell holds.
V_BLANK = "blank"
V_NUMBER = "number"
V_DATE = "date"
V_PERIOD = "period"
V_TEXT = "text"
V_ERROR = "error"
V_BOOL = "bool"
V_MARKER = "marker"        # -, n/a, TBD: a filled cell that says there is no value
VALUE_KINDS = (V_BLANK, V_NUMBER, V_DATE, V_PERIOD, V_TEXT, V_ERROR, V_BOOL, V_MARKER)
MIXED = "mixed"            # a column with no majority type
COLUMN_KINDS = (V_BLANK, V_NUMBER, V_DATE, V_PERIOD, V_TEXT, MIXED)

# A cell that does not fit its column's type, set apart by what it is and never lost.
STRAY_ERROR = "error"
STRAY_LABEL = "label"
STRAY_TOTAL = "total"
STRAY_REPEATED_HEADER = "repeated_header"
STRAY_MARKER = "marker"
STRAY_DATE = "date"
STRAY_NUMBER_WITH_WORD = "number_with_word"
STRAY_NOTE = "note"
STRAY_KINDS = (STRAY_ERROR, STRAY_LABEL, STRAY_TOTAL, STRAY_REPEATED_HEADER, STRAY_MARKER, STRAY_DATE, STRAY_NUMBER_WITH_WORD, STRAY_NOTE)

# What a number format says.
F_GENERAL = "general"
F_NUMBER = "number"
F_PERCENT = "percent"
F_CURRENCY = "currency"
F_DATE = "date"
F_TIME = "time"
F_DURATION = "duration"
F_TEXT = "text"
FORMAT_KINDS = (F_GENERAL, F_NUMBER, F_PERCENT, F_CURRENCY, F_DATE, F_TIME, F_DURATION, F_TEXT)

# What a period is.
P_DAY = "day"
P_MONTH = "month"
P_QUARTER = "quarter"
P_HALF = "half"
P_SEASON = "season"
P_YEAR = "year"
P_RANGE = "range"
P_RELATIVE = "relative"    # YTD, MTD, BOM, prompt
P_WEEKDAY = "weekday"
P_WEEK = "week"
P_HOUR = "hour"
PERIOD_KINDS = (P_DAY, P_MONTH, P_QUARTER, P_HALF, P_SEASON, P_YEAR, P_RANGE, P_RELATIVE, P_WEEKDAY, P_WEEK, P_HOUR)

# The shape of a formula, read from its text at its own address.
FX_AGG_COLUMN = "agg_column"       # adds or aggregates cells of its own column: the mark of a total line
FX_AGG_ROW = "agg_row"             # of its own row: the mark of a total column
FX_AGG_OTHER = "agg_other"
FX_AGG_RUN = "agg_run"             # an aggregate repeating down its column: a running balance, not a total
FX_COND_AGG = "cond_agg"
FX_ROW_ARITH = "row_arith"
FX_COLUMN_ARITH = "column_arith"
FX_COLUMN_RUN = "column_run"       # column arithmetic repeating down its column: =A5+1
FX_LINK = "link"
FX_LINK_COLUMN = "link_column"     # one cell of its own column
FX_OTHER_TAB = "other_tab"
FX_EXTERNAL = "external"
FX_LOOKUP = "lookup"
FX_IF = "if"
FX_DATE = "date"
FX_TEXT = "text"
FX_CONST = "const"
FX_MIXED = "mixed"
FX_VOLATILE = "volatile"           # reads the clock or a random draw (TODAY, NOW, RAND): its value moves with no edit, so it is never an as-of date and no part of a tab's hash
FORMULA_SHAPES = (FX_AGG_COLUMN, FX_AGG_ROW, FX_AGG_OTHER, FX_AGG_RUN, FX_COND_AGG, FX_ROW_ARITH, FX_COLUMN_ARITH, FX_COLUMN_RUN, FX_LINK, FX_LINK_COLUMN,
                  FX_OTHER_TAB, FX_EXTERNAL, FX_LOOKUP, FX_IF, FX_DATE, FX_TEXT, FX_CONST, FX_MIXED, FX_VOLATILE)

# What an axis of peer columns runs over.
AXIS_PERIOD = "period"
AXIS_SEQUENCE = "sequence"     # 1, 2, 3 ... across: hours of a day
AXIS_GROUP = "group"           # columns sharing a group label above them
AXIS_TOTALLED = "totalled"     # columns a total-named column stands beside: books, then their total, across spacers and totals
AXIS_FLAG = "flag"             # text columns marking the same few values: Y and N across
AXIS_KINDS = (AXIS_PERIOD, AXIS_SEQUENCE, AXIS_GROUP, AXIS_TOTALLED, AXIS_FLAG)

# The tab a host read.
SHEET_GRID = "grid"
SHEET_CHART = "chart"
SHEET_TYPES = (SHEET_GRID, SHEET_CHART)

# Why a line got its role. A line carries every signal that fired, so the role can be read back and checked.
S_TOTAL_WORD = "total_word"
S_FORMULA_TOTAL = "formula_total"              # aggregates of its own column make most of its figures
S_FORMULA_COMPUTED = "formula_computed"        # computed from lines of its own column, closing a block
S_FORMULA_CLOSES_BLOCK = "formula_closes_block"
S_ARITHMETIC_TIE = "arithmetic_tie"            # adds up by arithmetic, set apart by style
S_GROUP_TIE = "group_tie"                      # its figures are the sums of the other lines holding one of its texts in the same column: a group's subtotal
S_AUTOFILTER_TOP = "autofilter_top"
S_FROZEN_LAST = "frozen_last"
S_JOINED_ABOVE = "joined_above"                # a name line joined to the header under it
S_JOINED_BELOW = "joined_below"                # a units line joined to the header over it
S_MERGED_OVER_COLUMNS = "merged_over_columns"
S_ALONE_STYLED = "alone_styled"
S_ALONE_BEFORE_HEADER = "alone_before_header"
S_ALONE_AFTER_GAP = "alone_after_gap"
S_ABOVE_TABLE = "above_table"
S_BELOW_TABLE = "below_table"
S_LABEL_VALUE = "label_value"
S_FIGURES = "figures"
S_TEXT_LINE = "text_line"
S_NAMES_BODY = "names_body"                    # the name line directly above a body nothing else names
S_MODEL = "line_model"                         # the learned line model called it a header
S_RULE_LINE = "rule_line"                      # only rule characters, a spacer: never a name, a record or a band
S_TOTAL_WORD_IN_KEY = "total_word_in_key"      # on most lines the word sits inside the record's own key and ties to nothing: a record
S_TOTAL_OTHER_BLOCK = "total_other_block"      # a side-by-side neighbour's total on this line; this block's own cells hold a record
S_STRAY_KEY = "stray_key"                      # text where the key column holds dates, beside figures: a statistic printed under the data
S_DATA_LINE = "data_line"                      # called a header, but its cells fit the columns of the table it sits in
S_FILLED_DOWN = "filled_down"                  # a sparse label column's cell above fills a blank on this line
S_REPEATED_HEADER = "repeated_header"          # repeats its table's header
S_ALONE_AFTER_TOTAL = "alone_after_total"      # a lone label in the key column between a printed line and the next block's figures
S_STYLE_OF_HEADER = "style_of_header"          # the marks of a header above, set off from the line under it, on the same columns
S_KEY_OVER_KEYS = "key_over_keys"              # in a side-by-side list of keys, the key with a figure beside it heads the keys under it
LINE_SIGNALS = (S_TOTAL_WORD, S_FORMULA_TOTAL, S_FORMULA_COMPUTED, S_FORMULA_CLOSES_BLOCK, S_ARITHMETIC_TIE, S_GROUP_TIE, S_AUTOFILTER_TOP, S_FROZEN_LAST, S_JOINED_ABOVE, S_JOINED_BELOW, S_MERGED_OVER_COLUMNS, S_ALONE_STYLED,
                S_ALONE_BEFORE_HEADER, S_ALONE_AFTER_GAP, S_ABOVE_TABLE, S_BELOW_TABLE, S_LABEL_VALUE, S_FIGURES, S_TEXT_LINE, S_NAMES_BODY, S_MODEL, S_RULE_LINE, S_TOTAL_WORD_IN_KEY, S_TOTAL_OTHER_BLOCK, S_STRAY_KEY,
                S_DATA_LINE, S_FILLED_DOWN, S_REPEATED_HEADER, S_ALONE_AFTER_TOTAL, S_KEY_OVER_KEYS, S_STYLE_OF_HEADER)
