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
    if aggregate == "plain" and column == "*":
        return "every column"
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


OPERATION_WORDS = {"+": "plus", "-": "minus", "*": "times", "/": "divided by"}


def expression_words(expression):
    """A typed-AST value in the read-as line's words ("total gdp divided by total population"), or None for one
    the line has no words for."""
    from engine.sql_ast import Aggregate, BinaryExpr, ColumnRef, Literal, Lower, Star

    if isinstance(expression, ColumnRef):
        return words(expression.name)
    if isinstance(expression, Lower):
        return expression_words(expression.operand)
    if isinstance(expression, Aggregate):
        if isinstance(expression.operand, Star):
            return "number of rows"
        inner = expression_words(expression.operand)
        if inner and isinstance(expression.operand, BinaryExpr):
            inner = f"({inner})"                   # "total (amount times rate)", not "total amount times rate"
        return inner and f"{AGGREGATE_WORDS[expression.function]} {'distinct ' if expression.distinct else ''}{inner}"
    if isinstance(expression, BinaryExpr):
        sides = []
        for side in (expression.left, expression.right):
            text = expression_words(side)
            if text is None:
                return None
            sides.append(f"({text})" if isinstance(side, BinaryExpr) else text)
        return f"{sides[0]} {OPERATION_WORDS.get(expression.operator, expression.operator)} {sides[1]}"
    if isinstance(expression, Literal):
        return str(expression.value)
    return None


def predicate_words(predicate):
    """A typed-AST condition in the read-as line's words, or None for one the line has no words for."""
    from engine.sql_ast import BooleanExpr, Comparison, Literal

    if isinstance(predicate, Comparison):
        left = expression_words(predicate.left)
        right = (f"'{str(predicate.right.value).strip('%')}'" if isinstance(predicate.right, Literal)
                 and isinstance(predicate.right.value, str) else expression_words(predicate.right))
        return left and right and f"{left} {COMPARISONS.get(predicate.operator, predicate.operator.lower())} {right}"
    if isinstance(predicate, BooleanExpr):
        terms = [predicate_words(term) for term in predicate.terms]
        return None if None in terms else f" {predicate.operator.lower()} ".join(terms)
    return None


def query_words(query):
    """The read-as line of a query the readers did not build themselves, a registered calculation's expansion of
    their reading: what each output computes ("per capita (total gdp divided by total population) of economy"),
    under its conditions, for each group. None for a query the line has no words for, which then keeps the
    reading's own line."""
    shown = []
    for item in query.select:
        text = expression_words(item.expression)
        if text is None:
            return None
        shown.append(f"{words(item.alias)} ({text})" if item.alias and words(item.alias) != text else text)
    tables = [query.from_table, *(join.table for join in query.joins)]
    if not all(isinstance(table, str) for table in tables):
        return None
    line = ", ".join(shown) + " of " + " and ".join(words(table) for table in tables)
    if query.where is not None:
        conditions = predicate_words(query.where)
        if conditions is None:
            return None
        line += " where " + conditions
    if query.group_by:
        groups = [expression_words(column) for column in query.group_by]
        if None in groups:
            return None
        line += ", for each " + " and ".join(groups)
    return line


def read_as(readings):
    blocks = readings["blocks"]
    if readings["structure"] == "single":
        return block_words(blocks[0])
    if readings["structure"] == "NESTED":
        return block_words(blocks[0], inner=block_words(blocks[1]))
    return f"{block_words(blocks[0])}, {SETS[readings['structure']]} {block_words(blocks[1])}"
