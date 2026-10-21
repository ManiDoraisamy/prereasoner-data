"""The "read as" line: readings said in plain words, so a person sees what the question was taken
to ask before trusting the number. One sentence per block, in the readings' own terms:

    number of rows of singer where age is above 20
    name of singer, for each country, whose number of rows is at least 2, sorted by name lowest first
"""
from engine.readers.text import words

COMPARISONS = {"=": "is", "!=": "is not", ">": "is above", "<": "is below", ">=": "is at least", "<=": "is at most",
               "LIKE": "contains", "NOT LIKE": "does not contain", "IN": "is among", "NOT IN": "is not among"}
AGGREGATE_WORDS = {"COUNT": "count of", "SUM": "total", "AVG": "average", "MIN": "lowest", "MAX": "highest"}
SETS = {"UNION": "together with", "INTERSECT": "that are also", "EXCEPT": "except"}


def column_words(column):
    if column == "*":
        return "rows"
    part, _, rest = column.partition(":")
    if rest:
        return f"{part.replace('_', ' ')} of {words(rest.split('.', 1)[1])}"
    return words(column.split(".", 1)[1])


def item_words(aggregate, column, distinct=False):
    if aggregate == "plain":
        return column_words(column)
    if column == "*":
        return "number of rows"
    return f"{AGGREGATE_WORDS[aggregate]} {'distinct ' if distinct else ''}{column_words(column)}"


def value_words(value, inner):
    if isinstance(value, dict):
        return f"({inner})"
    if isinstance(value, str):
        return f"'{value.strip('%')}'"
    return str(value)


def conditions_words(conditions, conj, inner):
    joiner = " or " if conj == "OR" else " and "
    return joiner.join(f"{column_words(c)} {COMPARISONS[o]} {value_words(v, inner)}" for c, o, v in conditions)


def block_words(block, inner=None):
    shown = ", ".join(item_words(aggregate, column, distinct) for _, aggregate, column, distinct in block["select"])
    tables = " and ".join(words(t) for t in block["tables"])
    share = set(block.get("share") or ())
    if share:
        # A share says its part and its whole: "share of total amount of orders where city is 'Paris', out of total
        # amount of all orders". Read as the bare total, the line a person and the re-scorer read said no share.
        part = [w for i, w in enumerate(block["where"]) if i in share]
        whole = [w for i, w in enumerate(block["where"]) if i not in share]
        return (f"share of {shown} of {tables} where {conditions_words(part, 'AND', inner)}, out of {shown} of "
                + (f"{tables} where {conditions_words(whole, 'AND', inner)}" if whole else f"all {tables}"))
    text = ("distinct " if block["distinct"] else "") + shown + " of " + tables
    if block["where"]:
        text += " where " + conditions_words(block["where"], block["conj"], inner)
    if block["group"]:
        text += ", for each " + " and ".join(column_words(c) for c in block["group"])
    if block["having"]:
        text += ", whose " + " and ".join(f"{item_words(a, c)} {COMPARISONS[o]} {value_words(v, inner)}"
                                          for a, c, o, v in block["having"])
    if block["order"]:
        text += ", sorted by " + ", ".join(item_words(a, c) for a, c in block["order"])
        text += " highest first" if block["dir"] == "DESC" else " lowest first"
    if block["limit"]:
        text += ", the first" if block["limit"] == 1 else f", the first {block['limit']}"
    return text


def read_as(readings):
    blocks = readings["blocks"]
    if readings["structure"] == "single":
        return block_words(blocks[0])
    if readings["structure"] == "NESTED":
        return block_words(blocks[0], inner=block_words(blocks[1]))
    return f"{block_words(blocks[0])}, {SETS[readings['structure']]} {block_words(blocks[1])}"
