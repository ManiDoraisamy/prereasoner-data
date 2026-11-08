"""An interpretable ACE view of the readers' one typed meaning representation.

Syntax-tree productions follow Attempto/APE grammar.fit (5f4d5354a45fb772763bf1a9543f508f15b28982).
The vocabulary below is an application profile, not a claim to implement all ACE.
SQL construction remains engine.readers.readings.build.
"""
from __future__ import annotations

import json
import collections
import math

from engine.readers.decisions import BOUNDS, InvalidDecision
from engine.readers.readings import MAX_EXPLICIT_JOINS, MAX_JOIN_PREDICATES, SUBQUERY, NotReadable, build
from engine.sql_ast import validate_query, query_to_tree, query_from_tree

NOUNS = ("analysis", "block", "selection", "condition", "grouping", "ordering", "result", "join", "link")
VERBS = ("uses-structure", "has-block", "uses-position", "reads-table", "uses-distinct", "uses-conjunction",
         "uses-direction", "limits-to", "has-selection", "has-condition", "has-grouping", "has-having",
         "has-ordering", "uses-column", "uses-aggregate", "compares-with", "uses-value-kind", "uses-literal",
         "uses-share-part", "answers", "uses-root", "has-join", "uses-table", "uses-join-kind", "has-link",
         "uses-left-column", "uses-right-column")
# The canonical compiler view is wider than the current neural readout. Source
# programs can project nine fields, compare seven values and order three keys;
# encoding those programs does not imply the trained head can predict them.
ACE_BOUNDS = {**BOUNDS, "select": 16, "where": 16, "group": 8, "order": 8}
MAX_SENTENCES = 1024
MAX_LITERAL_CHARS = 4096


def node(label, *args):
    return {"node": label, **{f"arg{i}": arg for i, arg in enumerate(args, 1)}}


def declaration(noun, variable):
    det = "an" if noun[0] in "aeiou" else "a"
    return node("specification", node("top_s", node("topic", node("quant", "there is"),
                node("np", node("det", det), node("nbar", node("n", noun), node("appos", node("var", variable)))))), ".")


def fact(subject, verb, value, reference=False):
    obj = node("var", value) if reference else node("string", '"' + value + '"')
    return node("specification", node("s", node("np", node("var", subject)), node("vp", node("vbar",
                node("$empty"), node("vcompl", node("v", verb), node("np", obj)), node("$empty")))), ".")


def question():
    return node("query", node("question", node("np", node("qdet", "which"), node("nbar", node("n", "result"))),
                node("vp", node("vbar", node("$empty"), node("vcompl", node("v", "answers"),
                node("np", node("var", "A"))), node("$empty")))), "?")


def _quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def encode(reading):
    """Return ACE text and its actual APE-shaped JSON tree, from one meaning."""
    if (reading.get("structure") not in ("single", "UNION", "INTERSECT", "EXCEPT", "NESTED")
            or not isinstance(reading.get("blocks"), list)
            or len(reading["blocks"]) != (1 if reading["structure"] == "single" else 2)):
        raise InvalidDecision("ACE block count disagrees with structure")
    trees, sentences = [], []

    def append(tree, sentence):
        if len(trees) >= MAX_SENTENCES:
            raise InvalidDecision("ACE sentence bound exceeded")
        trees.append(tree)
        sentences.append(sentence)

    def declare(noun, var):
        append(declaration(noun, var), f"There is {'an' if noun[0] in 'aeiou' else 'a'} n:{noun} {var}.")

    def emit(subject, verb, value, ref=False):
        value = str(value)
        if len(value) > MAX_LITERAL_CHARS:
            raise InvalidDecision("ACE literal bound exceeded")
        try:
            value.encode("utf8")
        except UnicodeError as exc:
            raise InvalidDecision("invalid Unicode ACE literal") from exc
        append(fact(subject, verb, value, ref), f"{subject} v:{verb} {value if ref else _quote(value)}.")

    declare("analysis", "A")
    emit("A", "uses-structure", reading["structure"])
    for b, block in enumerate(reading["blocks"]):
        var = f"B{b}"
        declare("block", var)
        emit("A", "has-block", var, True)
        emit(var, "uses-position", b)
        for table in block["tables"]:
            emit(var, "reads-table", table)
        emit(var, "uses-distinct", str(block["distinct"]).lower())
        emit(var, "uses-conjunction", block["conj"])
        if block["dir"] is not None:
            emit(var, "uses-direction", block["dir"])
        if block["limit"] is not None:
            emit(var, "limits-to", block["limit"])
        if "root" in block or "joins" in block:
            if not isinstance(block.get("root"), str) or not isinstance(block.get("joins"), list):
                raise InvalidDecision("explicit ACE joins need a root and a join list")
            if len(block["joins"]) > MAX_EXPLICIT_JOINS:
                raise InvalidDecision("explicit ACE join bound exceeded")
            emit(var, "uses-root", block["root"])
            for j, join in enumerate(block["joins"]):
                if not isinstance(join, dict) or set(join) != {"table", "kind", "pairs"}:
                    raise InvalidDecision("invalid explicit ACE join")
                if not isinstance(join["pairs"], list) or not 1 <= len(join["pairs"]) <= MAX_JOIN_PREDICATES:
                    raise InvalidDecision("explicit ACE join predicate bound exceeded")
                child = f"J{b}{j}"
                declare("join", child)
                emit(var, "has-join", child, True)
                emit(child, "uses-position", j)
                emit(child, "uses-table", join["table"])
                emit(child, "uses-join-kind", join["kind"])
                for p, pair in enumerate(join["pairs"]):
                    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(c, str) for c in pair):
                        raise InvalidDecision("invalid explicit ACE join columns")
                    link = f"L{b}{j}{p}"
                    declare("link", link)
                    emit(child, "has-link", link, True)
                    emit(link, "uses-position", p)
                    emit(link, "uses-left-column", pair[0])
                    emit(link, "uses-right-column", pair[1])
        for kind, noun, verb, letter in (("select", "selection", "has-selection", "S"),
                                          ("where", "condition", "has-condition", "W"),
                                          ("group", "grouping", "has-grouping", "G"),
                                          ("having", "condition", "has-having", "H"),
                                          ("order", "ordering", "has-ordering", "O")):
            if len(block[kind]) > ACE_BOUNDS[kind]:
                raise InvalidDecision("ACE profile bound exceeded: " + kind)
            for i, term in enumerate(block[kind]):
                child = f"{letter}{b}{i}"
                declare(noun, child)
                emit(var, verb, child, True)
                emit(child, "uses-position", i)
                if kind == "select":
                    position, agg, col, distinct = term
                    if position != i:
                        raise InvalidDecision("noncanonical selection position")
                    emit(child, "uses-column", col)
                    emit(child, "uses-aggregate", agg)
                    emit(child, "uses-distinct", str(distinct).lower())
                elif kind == "group":
                    emit(child, "uses-column", term)
                elif kind == "order":
                    emit(child, "uses-column", term[1])
                    emit(child, "uses-aggregate", term[0])
                else:
                    col, op, value = term[-3:]
                    emit(child, "uses-column", col)
                    if kind == "having":
                        emit(child, "uses-aggregate", term[0])
                    emit(child, "compares-with", op)
                    if value == SUBQUERY:
                        vk, literal = "subquery", "1"
                    elif value is None:
                        vk, literal = "null", "null"
                    elif isinstance(value, bool):
                        vk, literal = "boolean", str(value).lower()
                    elif isinstance(value, (int, float)):
                        if isinstance(value, float) and not math.isfinite(value):
                            raise InvalidDecision("non-finite ACE literal")
                        vk, literal = "number", json.dumps(value, allow_nan=False)
                    elif isinstance(value, str):
                        vk, literal = "text", value
                    else:
                        raise InvalidDecision("unsupported ACE literal")
                    emit(child, "uses-value-kind", vk)
                    emit(child, "uses-literal", literal)
                    if kind == "where":
                        emit(child, "uses-share-part", str(i in block.get("share", [])).lower())
    append(question(), "Which n:result v:answers A?")
    return "\n".join(sentences), {"sentences": trees}


def _args(tree, label, size):
    keys = {"node", *(f"arg{i}" for i in range(1, size + 1))}
    if not isinstance(tree, dict) or set(tree) != keys or tree.get("node") != label:
        raise InvalidDecision("invalid ACE grammar node: " + label)
    return tuple(tree[f"arg{i}"] for i in range(1, size + 1))


def decode(document, graph):
    """Read the ACE tree, check canonical structure and references, build the AST."""
    if not isinstance(document, dict) or set(document) != {"sentences"}:
        raise InvalidDecision("invalid ACE document")
    sentences = document["sentences"]
    if not isinstance(sentences, list) or not 3 <= len(sentences) <= MAX_SENTENCES:
        raise InvalidDecision("ACE sentence count outside bounds")
    declared, facts = {}, {}
    for tree in sentences[:-1]:
        body, mark = _args(tree, "specification", 2)
        if mark != ".":
            raise InvalidDecision("specification must end in a period")
        if not isinstance(body, dict):
            raise InvalidDecision("invalid ACE sentence body")
        if body.get("node") == "top_s":
            topic, = _args(body, "top_s", 1)
            quant, np = _args(topic, "topic", 2)
            if _args(quant, "quant", 1) != ("there is",):
                raise InvalidDecision("unsupported ACE quantifier")
            det, nbar = _args(np, "np", 2)
            noun, appos = _args(nbar, "nbar", 2)
            noun, = _args(noun, "n", 1)
            var, = _args(appos, "appos", 1)
            var, = _args(var, "var", 1)
            if noun not in NOUNS or not isinstance(var, str) or var in declared:
                raise InvalidDecision("unknown noun or repeated declaration")
            if _args(det, "det", 1) != ("an" if noun[0] in "aeiou" else "a",):
                raise InvalidDecision("incorrect ACE article")
            declared[var], facts[var] = noun, {}
        else:
            np, vp = _args(body, "s", 2)
            subject, = _args(np, "np", 1)
            subject, = _args(subject, "var", 1)
            vbar, = _args(vp, "vp", 1)
            empty, compl, empty2 = _args(vbar, "vbar", 3)
            _args(empty, "$empty", 0)
            _args(empty2, "$empty", 0)
            verb, np = _args(compl, "vcompl", 2)
            verb, = _args(verb, "v", 1)
            obj, = _args(np, "np", 1)
            if not isinstance(subject, str) or subject not in declared or verb not in VERBS:
                raise InvalidDecision("undeclared subject or unknown ACE verb")
            if not isinstance(obj, dict):
                raise InvalidDecision("invalid ACE object")
            if obj.get("node") == "var":
                value, = _args(obj, "var", 1)
                if not isinstance(value, str) or value not in declared:
                    raise InvalidDecision("undeclared ACE reference")
            else:
                value, = _args(obj, "string", 1)
                if (not isinstance(value, str) or len(value) > MAX_LITERAL_CHARS + 2
                        or not (value.startswith('"') and value.endswith('"'))):
                    raise InvalidDecision("invalid ACE string token")
                value = value[1:-1]
                try:
                    value.encode("utf8")
                except UnicodeError as exc:
                    raise InvalidDecision("invalid Unicode ACE literal") from exc
            facts[subject].setdefault(verb, []).append(value)
    if sentences[-1] != question() or declared.get("A") != "analysis":
        raise InvalidDecision("missing canonical ACE query")

    def one(var, verb, default=None):
        values = facts[var].get(verb, [])
        if not values:
            return default
        if len(values) != 1:
            raise InvalidDecision("duplicate singleton ACE fact")
        return values[0]

    def boolean(var, verb):
        text = one(var, verb)
        if text not in ("true", "false"):
            raise InvalidDecision("invalid boolean ACE fact")
        return text == "true"

    reading = {"structure": one("A", "uses-structure"), "blocks": []}
    try:
        for b, var in enumerate(facts["A"].get("has-block", [])):
            if var != f"B{b}" or b > 1:
                raise InvalidDecision("invalid ACE block identity")
            limit = one(var, "limits-to")
            block = {"tables": facts[var].get("reads-table", []), "distinct": boolean(var, "uses-distinct"),
                     "conj": one(var, "uses-conjunction"), "dir": one(var, "uses-direction"),
                     "limit": int(limit) if limit is not None else None}
            root = one(var, "uses-root")
            if root is not None or facts[var].get("has-join"):
                block.update(root=root, joins=[
                    {"table": one(child, "uses-table"), "kind": one(child, "uses-join-kind"),
                     "pairs": [[one(link, "uses-left-column"), one(link, "uses-right-column")]
                               for link in facts[child].get("has-link", [])]}
                    for child in facts[var].get("has-join", [])])
            share = []
            for kind, verb in (("select", "has-selection"), ("where", "has-condition"), ("group", "has-grouping"),
                               ("having", "has-having"), ("order", "has-ordering")):
                block[kind] = []
                for i, child in enumerate(facts[var].get(verb, [])):
                    col, agg = one(child, "uses-column"), one(child, "uses-aggregate")
                    if kind == "select":
                        block[kind].append([i, agg, col, boolean(child, "uses-distinct")])
                    elif kind == "group":
                        block[kind].append(col)
                    elif kind == "order":
                        block[kind].append([agg, col])
                    else:
                        vk, value = one(child, "uses-value-kind"), one(child, "uses-literal")
                        if vk == "subquery":
                            if value != "1":
                                raise InvalidDecision("invalid subquery reference")
                            value = SUBQUERY
                        elif vk in ("number", "boolean", "null"):
                            value = json.loads(value)
                            valid = ((vk == "number" and (type(value) is int or (type(value) is float and math.isfinite(value))))
                                     or (vk == "boolean" and type(value) is bool) or (vk == "null" and value is None))
                            if not valid:
                                raise InvalidDecision("ACE literal type mismatch")
                        elif vk != "text":
                            raise InvalidDecision("unknown ACE literal kind")
                        block[kind].append(([agg] if kind == "having" else []) + [col, one(child, "compares-with"), value])
                        if kind == "where" and boolean(child, "uses-share-part"):
                            share.append(i)
            if share:
                block["share"] = share
            reading["blocks"].append(block)
        if encode(reading)[1] != document:
            raise InvalidDecision("noncanonical, inconsistent or unused ACE facts")
        if reading["structure"] not in ("single", "UNION", "INTERSECT", "EXCEPT", "NESTED"):
            raise InvalidDecision("invalid ACE query structure")
        if len(reading["blocks"]) != (1 if reading["structure"] == "single" else 2):
            raise InvalidDecision("ACE block count disagrees with structure")
        for block in reading["blocks"]:
            if not block["tables"] or not block["select"]:
                raise InvalidDecision("a block requires a table and a selected result")
            if bool(block["order"]) != (block["dir"] is not None):
                raise InvalidDecision("order terms and direction disagree")
        if reading["structure"] == "NESTED" and not any(
                term[-1] == SUBQUERY for kind in ("where", "having") for term in reading["blocks"][0][kind]):
            raise InvalidDecision("nested block is not referenced")
        if any(block.get("share") for block in reading["blocks"]):
            if reading["structure"] != "single" or any(block.get(name) for block in reading["blocks"]
                    for name in ("group", "having", "order", "limit")):
                raise InvalidDecision("a share cannot discard grouping, ordering or a row limit")
        query = build(reading, graph)
        validate_query(query)
        return reading, query
    except (KeyError, TypeError, ValueError, IndexError, NotReadable) as exc:
        raise InvalidDecision("ACE meaning rejected: " + str(exc)) from exc


def schema():
    """JSON Schema for the actual, restricted APE tree productions used above."""
    def pattern(value):
        if isinstance(value, dict):
            return {"type": "object", "properties": {k: pattern(v) for k, v in value.items()},
                    "required": list(value), "additionalProperties": False}
        if value == "@VAR":
            return {"type": "string", "pattern": "^(A|B[01]|[SWGHOJL][01][0-9]+)$"}
        if value == "@NOUN":
            return {"type": "string", "enum": list(NOUNS)}
        if value == "@VERB":
            return {"type": "string", "enum": list(VERBS)}
        if value == '"@STRING"':
            return {"type": "string", "maxLength": MAX_LITERAL_CHARS + 2, "pattern": '^"[\\s\\S]*"$'}
        return {"const": value}

    decl = declaration("analysis", "@VAR")
    decl["arg1"]["arg1"]["arg2"]["arg2"]["arg1"]["arg1"] = "@NOUN"
    decl_schema = pattern(decl)
    decl_schema["properties"]["arg1"]["properties"]["arg1"]["properties"]["arg2"]["properties"]["arg1"]["properties"]["arg1"] = {"enum": ["a", "an"]}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "properties": {"sentences": {"type": "array", "minItems": 3, "maxItems": MAX_SENTENCES,
                            "items": {"anyOf": [decl_schema, pattern(fact("@VAR", "@VERB", "@STRING")),
                                                 pattern(fact("@VAR", "@VERB", "@VAR", True)), pattern(question())]}}},
            "required": ["sentences"], "additionalProperties": False}


# Recursive view of the same typed AST. The bounded readings codec above remains
# its own input adapter; neither view constructs a second SQL representation.
QUERY_MAX_SENTENCES = 4096
QUERY_MAX_DEPTH = 128

def encode_query(query):
    document=query_to_tree(query);sentences=[];trees=[];counts=collections.Counter()
    def append(tree,text):
        if len(trees)>=QUERY_MAX_SENTENCES:raise InvalidDecision('recursive ACE sentence bound exceeded')
        trees.append(tree);sentences.append(text)
    def declare(noun,letter):
        number=counts[letter];counts[letter]+=1;var=letter+str(number)
        append(declaration(noun,var),f"There is {'an' if noun[0] in 'aeiou' else 'a'} n:{noun} {var}.")
        return var
    def emit(subject,verb,value,reference=False):
        value=str(value)
        if len(value)>MAX_LITERAL_CHARS:raise InvalidDecision('recursive ACE literal bound exceeded')
        value.encode('utf8')
        append(fact(subject,verb,value,reference),f'{subject} v:{verb} {value if reference else _quote(value)}.')
    append(declaration('analysis','A'),'There is an n:analysis A.')
    def scalar(field,value):
        kind='null' if value is None else 'boolean' if type(value) is bool else 'number' if type(value) in (int,float) else 'text'
        if kind=='text' and not isinstance(value,str):raise InvalidDecision('unexpected AST scalar')
        emit(field,'uses-value-kind',kind)
        emit(field,'uses-literal',value if kind=='text' else json.dumps(value,allow_nan=False))
    def assigned(field,value,depth):
        if isinstance(value,(dict,list)):emit(field,'has-value',visit(value,depth+1),True)
        else:scalar(field,value)
    def visit(value,depth):
        if depth>QUERY_MAX_DEPTH:raise InvalidDecision('recursive ACE depth exceeded')
        if isinstance(value,list):
            var=declare('list','L')
            for position,item in enumerate(value):
                field=declare('field','F');emit(var,'has-item',field,True);emit(field,'uses-position',position)
                assigned(field,item,depth)
            return var
        if not isinstance(value,dict):raise InvalidDecision('AST root/child must be an object or list')
        var=declare('expression','N') if 'node' in value else declare('object','O')
        if 'node' in value:emit(var,'uses-node-type',value['node'])
        for name in sorted(k for k in value if k!='node'):
            field=declare('field','F');emit(var,'has-field',field,True);emit(field,'uses-name',name)
            assigned(field,value[name],depth)
        return var
    root=visit(document,0);emit('A','has-root',root,True)
    append(question(),'Which n:result v:answers A?')
    return '\n'.join(sentences),{'sentences':trees}

def _query_leaves(tree):
    if not isinstance(tree,dict) or 'node' not in tree:raise InvalidDecision('malformed ACE syntax tree')
    if set(tree)=={'node','arg1'} and isinstance(tree['arg1'],str) and tree['node'] in ('n','v','var','string'):
        yield tree['node'],tree['arg1'];return
    for name,value in tree.items():
        if name!='node' and isinstance(value,dict):yield from _query_leaves(value)

def decode_query(document):
    if not isinstance(document,dict) or set(document)!={'sentences'}:raise InvalidDecision('invalid recursive ACE document')
    trees=document['sentences']
    if not isinstance(trees,list) or not 3<=len(trees)<=QUERY_MAX_SENTENCES:raise InvalidDecision('recursive ACE sentence count')
    if trees[-1]!=question():raise InvalidDecision('recursive ACE question differs')
    nouns={'analysis','expression','object','list','field'};declared={};facts=collections.defaultdict(lambda:collections.defaultdict(list))
    for tree in trees[:-1]:
        leaves=collections.defaultdict(list)
        for label,value in _query_leaves(tree):leaves[label].append(value)
        if leaves['n']:
            if len(leaves['n'])!=1 or len(leaves['var'])!=1:raise InvalidDecision('invalid ACE declaration')
            noun,var=leaves['n'][0],leaves['var'][0]
            if noun not in nouns or var in declared or tree!=declaration(noun,var):raise InvalidDecision('unrecognized or duplicate ACE declaration')
            declared[var]=noun;continue
        if len(leaves['v'])!=1 or len(leaves['var']) not in (1,2):raise InvalidDecision('invalid ACE fact')
        subject=leaves['var'][0];verb=leaves['v'][0]
        reference=len(leaves['var'])==2
        if reference:
            if leaves['string']:raise InvalidDecision('mixed ACE fact object')
            value=leaves['var'][1]
        else:
            if len(leaves['string'])!=1:raise InvalidDecision('missing ACE literal')
            raw=leaves['string'][0]
            if not raw.startswith('"') or not raw.endswith('"'):raise InvalidDecision('invalid ACE quoted literal')
            value=raw[1:-1]
        if tree!=fact(subject,verb,value,reference):raise InvalidDecision('noncanonical ACE production')
        facts[subject][verb].append((reference,value))
    if declared.get('A')!='analysis':raise InvalidDecision('missing analysis declaration')
    def one(subject,verb,ref=False):
        values=facts[subject].get(verb,[])
        if len(values)!=1 or values[0][0]!=ref:raise InvalidDecision('missing, duplicated or mistyped ACE fact: '+verb)
        return values[0][1]
    stack=set()
    def assigned(field,depth):
        if declared.get(field)!='field':raise InvalidDecision('expected ACE field')
        if 'has-value' in facts[field]:return visit(one(field,'has-value',True),depth+1)
        kind=one(field,'uses-value-kind');text=one(field,'uses-literal')
        if kind=='text':return text
        if kind not in ('null','number','boolean'):raise InvalidDecision('unknown ACE scalar kind')
        value=json.loads(text)
        if not ((kind=='null' and value is None) or (kind=='boolean' and type(value) is bool) or
                (kind=='number' and type(value) in (int,float))):raise InvalidDecision('ACE literal kind mismatch')
        return value
    def visit(var,depth):
        if depth>QUERY_MAX_DEPTH or var in stack:raise InvalidDecision('cyclic or excessive ACE graph')
        stack.add(var);noun=declared.get(var)
        try:
            if noun=='list':
                items=[]
                for reference,field in facts[var].get('has-item',[]):
                    if not reference:raise InvalidDecision('list item is not a reference')
                    position=one(field,'uses-position')
                    if position!=str(len(items)):raise InvalidDecision('noncanonical ACE list position')
                    items.append(assigned(field,depth))
                return items
            if noun not in ('expression','object'):raise InvalidDecision('unknown ACE object reference')
            result={'node':one(var,'uses-node-type')} if noun=='expression' else {}
            for reference,field in facts[var].get('has-field',[]):
                if not reference:raise InvalidDecision('field is not a reference')
                name=one(field,'uses-name')
                if name in result:raise InvalidDecision('duplicate ACE object field')
                result[name]=assigned(field,depth)
            return result
        finally:stack.remove(var)
    tree=visit(one('A','has-root',True),0)
    query=query_from_tree(tree)
    if encode_query(query)[1]!=document:raise InvalidDecision('unused, noncanonical or inconsistent ACE facts')
    return query
