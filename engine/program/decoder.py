"""The typed k-best over programs: scores come in as data on named coordinates, programs come out best first, each with the named contributions that scored it. No learned part: an encoder fills the same names and nothing here changes.

A frame is one program shape on one table: its fixed coordinates, then slots, each a list of options sorted by score. A program is one option from each slot, so a frame's score is additive and its programs enumerate best first by the lazy product search: pop the best tuple, push its
successors. All frames share one heap, ties break by frame order then slot positions, so the result never depends on hashing. Fewer conditions come first among equals, because their frames come first.

What is kept is design/program_language.txt's policy (Policy below): every table; every column of a slot's type, the top 50 of each type on a table over 50 columns; all values with one condition for show, agg and count, the top 50 per column for rank forms, the top 50 per column with two
conditions, the top 20 with three, on occurring combinations only; periods and numbers from the reading's own coordinates; calc as a contrast in one slot, never all pairs of programs. A condition beyond the third needs a strong score.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Sequence

from engine.program.catalog import Catalog, CatalogTable
from engine.program.index import Atom, TableIndex, index_of
from engine.program.language import (Agg, Calc, Col, Compare, CompareColumns, Condition, Contains, DateOf, Equals, InPeriod, InSection, IsBlank, IsNumber, OnLine, Program, Rank, RowArithmetic, SECTION, Show, Size, answer_type_of, is_sound, measure_coordinates, peers_name, period_from_name)
from engine.program.names import (A_COUNT, A_LIST, A_NAME, A_NUMBER, A_PERCENT, AGG, ANCHORS, ANYWHERE, AVG, BY_SIZE, CALC, CALC_OPS, COMPARISONS, COUNT, COUNT_DISTINCT, DIRECTIONS, EACH_BY_SIZE, ENTITY_GROUP, ENTITY_LINE, ENTITY_PEER, MAX, MIN, MINUS, NUMBER_AGGREGATES, OVER, PLUS, RANK, ROW_OPS, SHOW, SLOT_AGGREGATE,
                                  SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_LEFT, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_CONTAINS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FILTER, SLOT_FORM, SLOT_KEY, SLOT_LINE, SLOT_LISTING, SLOT_MEASURE, SLOT_NUMBER, SLOT_PEERS, SLOT_PERIOD, SLOT_ROW_OP, SLOT_SECTION,
                                  SLOT_SHOWN, SLOT_SIZE, SLOT_TABLE, SLOT_VALUE, STATED_BY_SIZE, SUM, coordinate, slot_of)
from engine.program.scoring import DEFAULT_WEIGHTS, Reading, ScoredProgram, Weights, score_program
from engine.sheet.names import PRINTED_ROLES

_LINE_GROUP = -2
_SECTION_GROUP = -3


@dataclass(frozen=True, slots=True)
class Policy:
    """How much of each slot is kept. Numbers from design/program_language.txt, section 3."""
    rank_values: int = 50          # per column, one condition on a rank form
    two_values: int = 50           # per column, two conditions
    three_values: int = 20         # per column, three conditions
    calc_values: int = 50          # per column, the contrast slot of a calc
    wide_table: int = 50           # over this many columns a slot keeps its top...
    wide_columns: int = 50         # ...this many of each type
    arithmetic_columns: int = 20   # number columns tried as the two sides of row arithmetic, by score
    strong_condition: float = 1.0  # past the policy's count of conditions, only conditions the reading scores this high
    compared_columns: int = 5      # columns tried against each other, by score, when a reading asks for a comparison
    max_pops: int = 4_000_000      # a hard stop, deterministic, on the search


POLICY = Policy()


@dataclass(frozen=True, slots=True)
class _Option:
    score: float
    payload: object


def _stated(atom: Atom) -> frozenset[str]:
    """What one condition uses up: a number the question states, its comparison word, or its asking for a blank, fills one condition, not two. A period may fill two: a start date and an end date in one month (98.2)."""
    return frozenset(name for name in atom.names if name.startswith(_ONCE))


def _periods(atom: Atom) -> frozenset[str]:
    return frozenset(name for name in atom.names if name.startswith(_PERIOD))


_ONCE = (f"{SLOT_NUMBER}:", f"{SLOT_BLANK}:", f"{SLOT_COMPARISON}:")
_PERIOD = f"{SLOT_PERIOD}:"


class _Node:
    """Some conditions already chosen, and the lines they keep."""

    __slots__ = ("chosen", "score", "mask", "groups", "stated", "periods", "full")

    def __init__(self, chosen: tuple[int, ...], score: float, mask: int | None, groups: frozenset[int], stated: frozenset[str], periods: frozenset[str] = frozenset(), full: frozenset[str] = frozenset()):
        self.chosen, self.score, self.mask, self.groups, self.stated, self.periods, self.full = chosen, score, mask, groups, stated, periods, full


class _Conditions:
    """Sets of k conditions on k distinct columns, best first, generated on demand from combinations that occur: "enumerate occurring combinations from the data, not from a cross product" (design/program_language.txt). Used as a slot: option i is the i-th best such set.

    A prefix tree over the conditions in score order. A node's next conditions are those later in the order, on another column, that keep a line with it, computed only when the node is reached, so a prefix that keeps few lines costs one AND per condition and rules most of them out at once. A heap holds each node's next child at an upper bound of its best completion. Bitmasks make the check one AND."""

    def __init__(self, atoms: list[_Option], count: int, index: TableIndex, table: CatalogTable, score_of: Callable[[tuple[Atom, ...]], float], *, any_line: bool = False):
        self.atoms = atoms
        self.any_line = any_line
        self.scores = [option.score for option in atoms]
        self.facts: list[tuple | None] = [None] * len(atoms)
        self.total: int | None = None
        self.count = count
        self.index = index
        self.table = table
        self.score_of = score_of
        self.found: list[_Option] = []
        self.heap: list[tuple] = []
        self.sequence = 0
        self.groups = len({option.payload.group for option in atoms})
        if self.groups >= count:
            self._offer(_Node((), 0.0, None, frozenset(), frozenset()))

    def _completion(self, node: _Node, at: int) -> float | None:
        """The best any set through position at under node can score, or None when too few conditions follow it."""
        needed = self.count - len(node.chosen)
        if at + needed > len(self.scores):
            return None
        return node.score + sum(self.scores[at:at + needed])

    def _offer(self, node: _Node, at: int | None = None) -> None:
        """A node's next condition, from position at on: pushed at the best its position allows, and read only when it comes up, so a prefix reached late costs nothing."""
        if at is None:
            at = node.chosen[-1] + 1 if node.chosen else 0
        bound = self._completion(node, at)
        if bound is not None:
            self.sequence += 1
            heapq.heappush(self.heap, (-bound, self.sequence, node, at))

    def _facts(self, at: int) -> tuple:
        """A condition's column, what it uses up, its period and its lines, read once."""
        held = self.facts[at]
        if held is None:
            atom = self.atoms[at].payload
            held = self.facts[at] = (atom.group, _stated(atom), _periods(atom), self.index.mask(atom.condition))
        return held

    def _fits(self, node: _Node, at: int) -> bool:
        group, stated, periods, mask = self._facts(at)
        if group in node.groups or (node.stated and stated and not node.stated.isdisjoint(stated)) or (node.full and periods and not node.full.isdisjoint(periods)):
            return False
        return node.mask is None or bool(node.mask & mask)

    def _next(self) -> bool:
        while self.heap:
            negative, _, node, at = heapq.heappop(self.heap)
            if isinstance(node, tuple):
                self.found.append(_Option(-negative, node))
                return True
            limit = len(self.atoms)
            start = at
            while at < limit and not self._fits(node, at):
                at += 1
            if at >= limit:
                continue
            if at != start:
                self._offer(node, at)                      # the first that fits sits lower: it waits its turn at its own bound
                continue
            before = len(self.found)
            self._take(node, at, -negative)
            if len(self.found) > before:
                return True
        return False

    def _take(self, node: _Node, at: int, bound: float) -> None:
        self._offer(node, at + 1)
        option = self.atoms[at]
        chosen = node.chosen + (at,)
        if len(chosen) == self.count:
            atoms = tuple(self.atoms[held].payload for held in chosen)
            if not _occurring(self.index, self.table, atoms, self.any_line):
                return
            exact = self.score_of(atoms)
            if exact < bound - 1e-12:
                self.sequence += 1
                heapq.heappush(self.heap, (-exact, self.sequence, atoms, 0))     # a coordinate two conditions share counts once: it goes back at its true score
                return
            self.found.append(_Option(exact, atoms))
            return
        group, stated, periods, lines = self._facts(at)
        mask = lines if node.mask is None else node.mask & lines
        if mask:
            self._offer(_Node(chosen, node.score + option.score, mask, node.groups | {group}, node.stated | stated, node.periods | periods, node.full | (node.periods & periods)))

    def bound(self) -> float | None:
        """An upper bound on every set not yet found, or None when none is left."""
        return -self.heap[0][0] if self.heap else None

    def has(self, i: int) -> bool:
        while len(self.found) <= i:
            if not self._next():
                return False
        return True

    def __getitem__(self, i: int) -> _Option:
        self.has(i)
        return self.found[i]

    def size(self) -> int:
        """How many sets the columns allow, occurring or not: the elementary symmetric polynomial of the column sizes."""
        if self.total is None:
            self.total = self._count()
        return self.total

    def _count(self) -> int:
        sizes: dict[int, int] = {}
        for option in self.atoms:
            sizes[option.payload.group] = sizes.get(option.payload.group, 0) + 1
        totals = [1] + [0] * self.count
        for size in sizes.values():
            for k in range(self.count, 0, -1):
                totals[k] += totals[k - 1] * size
        return totals[self.count]


@dataclass(slots=True)
class _Frame:
    base: float
    slots: tuple
    build: Callable[[tuple], Program | None]


@dataclass(frozen=True, slots=True)
class Decoded:
    """The top programs, and what it took: the size of the space the policy kept, frames, and tuples popped."""
    programs: tuple[ScoredProgram, ...]
    candidates: int
    frames: int
    pops: int


class Decoder:
    """Decodes questions over one catalog. The index of each table is built once, at index time."""

    def __init__(self, catalog: Catalog, *, policy: Policy = POLICY):
        self.catalog = catalog
        self.policy = policy
        self.indexes = {table.id: index_of(table) for table in catalog.tables}

    def decode(self, reading: Reading, *, k: int = 50, weights: Weights = DEFAULT_WEIGHTS) -> Decoded:
        """The top k programs, best first, each scored and its contributions named."""
        programs, candidates, frames, pops = self.enumerate(reading, k=k, weights=weights)
        scored = [score_program(program, reading, self.catalog, weights) for program in programs]
        order = sorted(range(len(scored)), key=lambda at: (-scored[at].score, len(scored[at].contributions), at))
        return Decoded(tuple(scored[at] for at in order), candidates, frames, pops)

    def enumerate(self, reading: Reading, *, k: int = 50, weights: Weights = DEFAULT_WEIGHTS) -> tuple[list[Program], int, int, int]:
        """The search alone: programs in the order the frames give them, the size of the kept space, the frames, the tuples popped."""
        search = _Search(self, reading, weights)
        frames: list[_Frame] = []
        for table in self.catalog.tables:
            frames.extend(search.table_frames(table))
        frames.extend(search.cross_table_frames())
        return _best(frames, k, self.policy)


def _size(slot) -> int:
    return slot.size() if isinstance(slot, _Conditions) else len(slot)


def _bound(frame: _Frame, indices: tuple[int, ...]) -> float | None:
    """A tuple's score, or an upper bound on it while a lazy slot has not reached that far; None when a slot cannot hold it."""
    total = frame.base
    for slot, i in zip(frame.slots, indices):
        if isinstance(slot, _Conditions):
            if i < len(slot.found):
                total += slot.found[i].score
            else:
                bound = slot.bound()
                if bound is None:
                    return None
                total += bound
        elif i < len(slot):
            total += slot[i].score
        else:
            return None
    return total


def _best(frames: list[_Frame], k: int, policy: Policy) -> tuple[list[Program], int, int, int]:
    heap: list[tuple[float, int, tuple[int, ...]]] = []
    candidates = 0
    for at, frame in enumerate(frames):
        count = 1
        for slot in frame.slots:
            count *= _size(slot)
        candidates += count
        if count:
            root = (0,) * len(frame.slots)
            bound = _bound(frame, root)
            if bound is not None:
                heapq.heappush(heap, (-bound, at, root))
    visited: set[tuple[int, tuple[int, ...]]] = set()
    seen: set[Program] = set()
    out: list[Program] = []
    pops = 0
    while heap and len(out) < k and pops < policy.max_pops:
        negative, at, indices = heapq.heappop(heap)
        pops += 1
        frame = frames[at]
        if not all(slot.has(i) for slot, i in zip(frame.slots, indices) if isinstance(slot, _Conditions)):
            continue
        real = _score(frame, indices)
        if real < -negative - 1e-12:
            heapq.heappush(heap, (-real, at, indices))
            continue
        for j in range(len(indices)):
            following = indices[:j] + (indices[j] + 1,) + indices[j + 1:]
            if (at, following) not in visited:
                bound = _bound(frame, following)
                if bound is not None:
                    visited.add((at, following))
                    heapq.heappush(heap, (-bound, at, following))
        program = frame.build(tuple(frame.slots[j][i].payload for j, i in enumerate(indices)))
        if program is None or program in seen or not is_sound(program):
            continue
        seen.add(program)
        out.append(program)
    return out, candidates, len(frames), pops


def _score(frame: _Frame, indices: tuple[int, ...]) -> float:
    return frame.base + sum(frame.slots[j][i].score for j, i in enumerate(indices))


class _Search:
    """One question's frames."""

    def __init__(self, decoder: Decoder, reading: Reading, weights: Weights):
        self.decoder = decoder
        self.reading = reading
        self.weights = weights
        self.policy = decoder.policy
        self.slots: dict[tuple, object] = {}
        self.by_slot: dict[str, list[str]] = {}
        for name in sorted(reading.scores):
            self.by_slot.setdefault(slot_of(name), []).append(name)

    def s(self, name: str) -> float:
        return self.weights.of(slot_of(name)) * self.reading.score(name)

    def positive(self, slot: str) -> list[str]:
        return [name for name in self.by_slot.get(slot, ()) if self.reading.score(name) > 0]

    def type_score(self, kind: str) -> float:
        return self.s(coordinate(SLOT_ANSWER_TYPE, kind))

    # ------------------------------------------------------------------------------------------- conditions
    def atoms_of(self, table: CatalogTable, index: TableIndex) -> list[_Option]:
        """Every condition for this table and reading, best first: the sheet's atoms, and those the reading's own coordinates state."""
        out: dict[Condition, Atom] = {}
        for atom in index.atoms:
            out[atom.condition] = atom
        for atom in self.reading_atoms(table, index):
            out.setdefault(atom.condition, atom)
        options = [_Option(self.atom_score(atom), atom) for atom in out.values()]
        order = sorted(range(len(options)), key=lambda at: (-options[at].score, at))
        return [options[at] for at in order]

    def atom_score(self, atom: Atom) -> float:
        return sum(self.s(name) for name in atom.names)

    def without_shared(self, held: list[_Option]) -> list[_Option]:
        """A column's conditions for the second side of a contrast: the coordinate both sides fill, the column's own, counts once."""
        shared = set(held[0].payload.names)
        for option in held:
            shared &= set(option.payload.names)
        taken = sum(self.s(name) for name in shared)
        return [_Option(option.score - taken, option.payload) for option in held]

    def left_atoms(self, held: list[_Option], *, shared_only: bool = False) -> list[_Option]:
        """Conditions as a calc's left side reads them: what only that side fills is named as the left's, since a - b is not b - a."""
        if not self.positive(SLOT_CALC_LEFT):
            return held
        shared: set[str] = set()
        if shared_only:
            shared = set(held[0].payload.names)
            for option in held:
                shared &= set(option.payload.names)
        options = [_Option(option.score + sum(self.s(coordinate(SLOT_CALC_LEFT, name)) for name in option.payload.names if name not in shared), option.payload) for option in held]
        order = sorted(range(len(options)), key=lambda at: (-options[at].score, at))
        return [options[at] for at in order]

    def left_columns(self, held: list[_Option], slot: str) -> list[_Option]:
        """Columns or measures as a calc's left side reads them."""
        if not self.positive(SLOT_CALC_LEFT):
            return held
        options = []
        for option in held:
            names = measure_coordinates(option.payload) if slot == SLOT_MEASURE else [coordinate(slot, option.payload.name)]
            options.append(_Option(option.score + sum(self.s(coordinate(SLOT_CALC_LEFT, name)) for name in names), option.payload))
        order = sorted(range(len(options)), key=lambda at: (-options[at].score, at))
        return [options[at] for at in order]

    def set_score(self, atoms: tuple[Atom, ...]) -> float:
        return sum(self.s(name) for name in {name for atom in atoms for name in atom.names})

    def reading_atoms(self, table: CatalogTable, index: TableIndex) -> list[Atom]:
        """Conditions only a reading can state: a contained phrase, a period, a stated number, a blank, a value no cell list holds, a label or band read in part."""
        out: list[Atom] = []
        prefix = f"{table.id}."
        for name in self.positive(SLOT_CONTAINS):
            body = name.split(":", 1)[1]
            if not body.startswith(prefix):
                continue
            position, _, rest = body[len(prefix):].partition("=")
            text, anchor = rest, ANYWHERE
            head, _, tail = rest.rpartition("=")
            if head and tail in ANCHORS:
                text, anchor = head, tail
            out.append(index.atom_of(Contains(Col(table.id, int(position)), text, anchor), int(position)))
        for name in self.positive(SLOT_VALUE):
            body = name.split(":", 1)[1]
            if not body.startswith(prefix):
                continue
            position, _, value = body[len(prefix):].partition("=")
            position = int(position)
            if position < 0 or position >= len(table.columns):
                continue
            number = _number_or_none(value)
            if number is not None and position in index.number_columns and table.columns[position].kind == "number":
                out.append(index.atom_of(Equals(Col(table.id, position), number), position))
            else:
                out.append(index.atom_of(Equals(Col(table.id, position), value), position))
        for name in self.positive(SLOT_LINE):
            body = name.split(":", 1)[1]
            owner, _, rest = body.partition("=")
            if owner != table.id:
                continue
            text, role = rest, None
            head, _, tail = rest.rpartition("=")
            if tail in PRINTED_ROLES and "=" in rest:
                text, role = head, tail
            out.append(index.atom_of(OnLine(table.id, text, role), _LINE_GROUP))
        for name in self.positive(SLOT_SECTION):
            owner, _, text = name.split(":", 1)[1].partition("=")
            if owner == table.id:
                out.append(index.atom_of(InSection(table.id, text, text not in index.bands), _SECTION_GROUP))
        periods = [period_from_name(name.split(":", 1)[1]) for name in self.positive(SLOT_PERIOD)]
        if periods:
            # a column the sheet types as dates; or one the reading itself asks to filter that holds any day
            named = [position for position, days in enumerate(table.days) if position not in index.date_columns and self.s(coordinate(SLOT_FILTER, Col(table.id, position).name)) > 0 and any(days)]
            for position in (*index.date_columns, *named):
                col = Col(table.id, position)
                out.extend(index.atom_of(InPeriod(col, span), position) for span in periods)
        numbers = [float(name.split(":", 1)[1]) for name in self.positive(SLOT_NUMBER)]
        if numbers:
            asked = list(dict.fromkeys(name.split(":", 1)[1].split("@", 1)[0] for name in self.positive(SLOT_COMPARISON))) or COMPARISONS
            for position in index.number_columns:
                col = Col(table.id, position)
                for number in numbers:
                    out.extend(index.atom_of(Compare(col, op, number), position) for op in asked)
        for comparison in self.positive(SLOT_COMPARISON):
            op = comparison.split(":", 1)[1]
            if "@" in op:
                continue
            scored = sorted((position for position in index.number_columns if self.s(coordinate(SLOT_FILTER, Col(table.id, position).name)) > 0), key=lambda position: (-self.s(coordinate(SLOT_FILTER, Col(table.id, position).name)), position))[:self.policy.compared_columns]
            for a in scored:
                out.extend(index.atom_of(CompareColumns(Col(table.id, a), op, Col(table.id, b)), a) for b in scored if a != b)
        for blank in self.positive(SLOT_BLANK):
            word = blank.split(":", 1)[1]
            if word == "number":
                out.extend(index.atom_of(IsNumber(Col(table.id, position)), position) for position in index.number_columns if self.s(coordinate(SLOT_FILTER, Col(table.id, position).name)) > 0)
            else:
                out.extend(index.atom_of(IsBlank(Col(table.id, position), filled=word == "filled"), position) for position in range(len(table.columns)))
        return out

    def per_column(self, atoms: list[_Option], keep: int) -> list[_Option]:
        """The top options of each column, in overall order."""
        counts: dict[int, int] = {}
        out = []
        for option in atoms:
            group = option.payload.group
            held = counts.get(group, 0)
            if held < keep:
                counts[group] = held + 1
                out.append(option)
        return out

    # ------------------------------------------------------------------------------------------------ slots
    def column_options(self, table: CatalogTable, positions: Sequence[int], slot: str, typed: bool = False) -> list[_Option]:
        """Columns of one type for one slot, best first; a wide table keeps its top ones."""
        options = []
        for position in positions:
            col = Col(table.id, position)
            score = self.s(coordinate(slot, col.name))
            if typed:
                score += self.type_score(answer_type_of(Show(table.id, (col,)), self.decoder.catalog.column_kind))
            options.append(_Option(score, col))
        order = sorted(range(len(options)), key=lambda at: (-options[at].score, at))
        keep = self.policy.wide_columns if len(table.columns) > self.policy.wide_table else len(options)
        return [options[at] for at in order[:keep]]

    def measure_options(self, table: CatalogTable, index: TableIndex, *, dates: bool = False, arithmetic: bool = True) -> list[_Option]:
        """Measures: a number column; two under an operator; a size; for a rank, a date column read as its day."""
        columns = self.column_options(table, index.number_columns, SLOT_MEASURE)
        options = list(columns)
        size = self.s(coordinate(SLOT_SIZE, EACH_BY_SIZE))
        options.extend(_Option(option.score + size, Size(option.payload)) for option in columns)
        if arithmetic and len(columns) >= 2:
            top = columns[:self.policy.arithmetic_columns]
            for op in ROW_OPS:
                if coordinate(SLOT_ROW_OP, op) not in self.reading.scores:
                    continue              # arithmetic is read only where the reading scores it: two named columns are not by themselves a sum of them
                weight = self.s(coordinate(SLOT_ROW_OP, op))
                for a in top:
                    for b in top:
                        if a.payload != b.payload and (op in (MINUS, OVER) or a.payload < b.payload):
                            options.append(_Option(a.score + b.score + weight, RowArithmetic(a.payload, op, b.payload)))
        if dates:
            options.extend(_Option(self.s(coordinate(SLOT_MEASURE, Col(table.id, position).name)), DateOf(Col(table.id, position))) for position in index.date_columns)
        order = sorted(range(len(options)), key=lambda at: (-options[at].score, at))
        return [options[at] for at in order]

    def shown_options(self, table: CatalogTable, positions: Sequence[int]) -> list[_Option]:
        """One shown column, or several when the reading names several: only columns it scores positively combine."""
        singles = self.column_options(table, positions, SLOT_SHOWN, typed=True)
        options = [_Option(option.score, (option.payload,)) for option in singles]
        named = [option for option in singles if self.reading.score(coordinate(SLOT_SHOWN, option.payload.name)) > 0][:8]
        for size in (2, 3):
            for group in combinations(sorted(named, key=lambda option: option.payload.position), size):
                options.append(_Option(sum(option.score for option in group), tuple(option.payload for option in group)))
        order = sorted(range(len(options)), key=lambda at: (-options[at].score, at))
        return [options[at] for at in order]

    # ------------------------------------------------------------------------------------------------ frames
    def condition_slot(self, table: CatalogTable, index: TableIndex, atoms: list[_Option], count: int, rank: bool, any_line: bool = False):
        """The sets of count conditions the policy keeps, or None: all values with one condition, the top 50 of a column for a rank form; the top 50 with two; the top 20 with three; beyond that only what the reading scores strongly."""
        key = (table.id, count, rank and count == 1, any_line)
        held = self.slots.get(key, False)
        if held is not False:
            return held
        if count == 1:
            keep = self.per_column(atoms, self.policy.rank_values) if rank else atoms
            slot = [_Option(option.score, (option.payload,)) for option in keep]
        else:
            keep = self.per_column(atoms, self.policy.two_values if count == 2 else self.policy.three_values) if count <= 3 else [option for option in atoms if option.score >= self.policy.strong_condition]
            slot = _Conditions(keep, count, index, table, self.set_score, any_line=any_line)
        self.slots[key] = slot
        return slot

    def condition_frames(self, base: float, slots: tuple, build, table: CatalogTable, index: TableIndex, atoms: list[_Option], *, min_conditions: int = 0, max_policy: int = 3, rank: bool = False, any_line: bool = False) -> list[_Frame]:
        """One frame per number of conditions, fewest first. Past max_policy, only conditions the reading scores strongly."""
        frames = []
        for count in range(min_conditions, 7):
            total = base + self.s(coordinate(SLOT_CONDITIONS, table.id, count))
            if count == 0:
                frames.append(_Frame(total, slots, lambda options, b=build: b(options, ())))
                continue
            if count > max_policy and count <= 3:
                slot = _Conditions([option for option in atoms if option.score >= self.policy.strong_condition], count, index, table, self.set_score, any_line=any_line)
            else:
                slot = self.condition_slot(table, index, atoms, count, rank, any_line)
            if isinstance(slot, _Conditions) and slot.groups < count:
                continue
            frames.append(_Frame(total, slots + (slot,), lambda options, b=build: b(options[:-1], tuple(atom.condition for atom in options[-1]))))
        return frames

    def table_frames(self, table: CatalogTable) -> list[_Frame]:
        index = self.decoder.indexes[table.id]
        if not table.lines:
            return []
        atoms = self.atoms_of(table, index)
        frames: list[_Frame] = []
        table_score = self.s(coordinate(SLOT_TABLE, table.id))
        all_positions = [column.position for column in table.columns]
        s = self.s
        # show one line
        form = s(coordinate(SLOT_FORM, SHOW)) + table_score
        shown = self.shown_options(table, all_positions + ([SECTION] if any(line.section for line in table.lines) else []))
        frames += self.condition_frames(form, (shown,), lambda options, where: Show(table.id, options[0], where), table, index, atoms, min_conditions=1, any_line=True)
        # show a list
        listed = form + s(coordinate(SLOT_LISTING, "yes")) + self.type_score(A_LIST)
        columns = self.column_options(table, all_positions, SLOT_SHOWN)
        frames += self.condition_frames(listed, (columns,), lambda options, where: Show(table.id, (options[0],), where, listing=True), table, index, atoms, max_policy=2)
        # agg
        agg = s(coordinate(SLOT_FORM, AGG)) + table_score
        measures = self.measure_options(table, index)
        for function in NUMBER_AGGREGATES:
            base = agg + s(coordinate(SLOT_AGGREGATE, function)) + self.type_score(A_NUMBER)
            frames += self.condition_frames(base, (measures,), lambda options, where, f=function: Agg(f, options[0], table.id, where), table, index, atoms)
        magnitude = agg + s(coordinate(SLOT_AGGREGATE, SUM)) + self.type_score(A_NUMBER) + s(coordinate(SLOT_SIZE, STATED_BY_SIZE))
        plain = self.column_options(table, index.number_columns, SLOT_MEASURE)
        frames += self.condition_frames(magnitude, (plain,), lambda options, where: Agg(SUM, options[0], table.id, where, magnitude=True), table, index, atoms)
        count = agg + s(coordinate(SLOT_AGGREGATE, COUNT)) + self.type_score(A_COUNT)
        frames += self.condition_frames(count, (), lambda options, where: Agg(COUNT, None, table.id, where), table, index, atoms)
        distinct = agg + s(coordinate(SLOT_AGGREGATE, COUNT_DISTINCT)) + self.type_score(A_COUNT)
        keys = self.column_options(table, all_positions, SLOT_MEASURE)
        frames += self.condition_frames(distinct, (keys,), lambda options, where: Agg(COUNT_DISTINCT, options[0], table.id, where), table, index, atoms, max_policy=2)
        # rank
        rank = s(coordinate(SLOT_FORM, RANK)) + table_score
        rank_measures = self.measure_options(table, index, dates=True)
        group_keys = self.column_options(table, all_positions + ([SECTION] if any(line.section for line in table.lines) else []), SLOT_KEY, typed=True)
        for direction in DIRECTIONS:
            turn = rank + s(coordinate(SLOT_DIRECTION, direction))
            line = turn + s(coordinate(SLOT_ENTITY, ENTITY_LINE))
            frames += self.condition_frames(line, (shown, rank_measures), lambda options, where, d=direction: Rank(ENTITY_LINE, table.id, d, where, options[1], options[0]), table, index, atoms, rank=True)
            group = turn + s(coordinate(SLOT_ENTITY, ENTITY_GROUP))
            for function in (SUM, AVG, MIN, MAX):
                base = group + s(coordinate(SLOT_AGGREGATE, function))
                frames += self.condition_frames(base, (group_keys, measures if function == SUM else plain), lambda options, where, d=direction, f=function: Rank(ENTITY_GROUP, table.id, d, where, options[1], key=options[0], function=f), table, index, atoms, rank=True, max_policy=2)
            sized = group + s(coordinate(SLOT_AGGREGATE, SUM)) + s(coordinate(SLOT_SIZE, BY_SIZE))
            frames += self.condition_frames(sized, (group_keys, plain), lambda options, where, d=direction: Rank(ENTITY_GROUP, table.id, d, where, options[1], key=options[0], function=SUM, by_size=True), table, index, atoms, rank=True, max_policy=2)
            counted = group + s(coordinate(SLOT_AGGREGATE, COUNT))
            frames += self.condition_frames(counted, (group_keys,), lambda options, where, d=direction: Rank(ENTITY_GROUP, table.id, d, where, None, key=options[0], function=COUNT), table, index, atoms, rank=True, max_policy=2)
            peer_sets = self.peer_options(table, index)
            if peer_sets:
                peer = turn + s(coordinate(SLOT_ENTITY, ENTITY_PEER)) + self.type_score(A_NAME)
                frames += self.condition_frames(peer, (peer_sets,), lambda options, where, d=direction: Rank(ENTITY_PEER, table.id, d, where, peers=options[0]), table, index, atoms, rank=True, max_policy=1)
                frames += self.condition_frames(peer + s(coordinate(SLOT_SIZE, BY_SIZE)), (peer_sets,), lambda options, where, d=direction: Rank(ENTITY_PEER, table.id, d, where, peers=options[0], by_size=True), table, index, atoms, rank=True, max_policy=1)
                marks = [_Option(s(coordinate(SLOT_VALUE, table.id, mark)) + s(coordinate(SLOT_PEERS, table.id, peers_name(index.mark_columns(mark)))), mark) for mark in index.peer_marks]
                marks = sorted(marks, key=lambda option: -option.score)
                frames += self.condition_frames(peer, (peer_sets, marks), lambda options, where, d=direction: Rank(ENTITY_PEER, table.id, d, where, peers=options[0], mark=options[1]), table, index, atoms, rank=True, max_policy=0)
                frames += self.condition_frames(peer, (marks,), lambda options, where, d=direction: Rank(ENTITY_PEER, table.id, d, where, peers=index.mark_columns(options[0]), mark=options[0]), table, index, atoms, rank=True, max_policy=0)
        frames += self.calc_frames(table, index, atoms, measures, plain)
        return frames

    def peer_options(self, table: CatalogTable, index: TableIndex) -> list[_Option]:
        sets = list(index.peer_sets)
        for name in self.positive(SLOT_PEERS):
            owner, _, positions = name.split(":", 1)[1].partition("=")
            if owner == table.id:
                held = tuple(int(position) for position in positions.split(","))
                if held not in sets:
                    sets.append(held)
        options = [_Option(self.s(coordinate(SLOT_PEERS, table.id, peers_name(held))), held) for held in sets]
        order = sorted(range(len(options)), key=lambda at: (-options[at].score, at))
        return [options[at] for at in order]

    def calc_frames(self, table: CatalogTable, index: TableIndex, atoms: list[_Option], measures: list[_Option], plain: list[_Option]) -> list[_Frame]:
        """Calc as a contrast: two sides differing in one slot. Spent only when the reading scores calc at all."""
        s = self.s
        form = s(coordinate(SLOT_FORM, CALC))
        if coordinate(SLOT_FORM, CALC) not in self.reading.scores:
            return []
        frames: list[_Frame] = []
        table_score = s(coordinate(SLOT_TABLE, table.id))
        base = form + table_score
        selectors = [self.condition_slot(table, index, atoms, count, True) for count in (1, 2, 3)]
        read_lines = [self.condition_slot(table, index, atoms, count, True, any_line=True) for count in (1, 2)]

        def conditions(count: int) -> float:
            return s(coordinate(SLOT_CONDITIONS, table.id, count))

        contrast = self.per_column(atoms, self.policy.calc_values)
        by_column: dict[int, list[_Option]] = {}
        for option in contrast:
            by_column.setdefault(option.payload.group, []).append(option)
        groups = [(self.left_atoms(held, shared_only=True), self.without_shared(held)) for group, held in sorted(by_column.items()) if len(held) >= 2]
        aggregate = s(coordinate(SLOT_AGGREGATE, SUM))
        shown_numbers = self.column_options(table, index.number_columns, SLOT_SHOWN)
        left_shown = self.left_columns(shown_numbers, SLOT_SHOWN)
        left_measures = self.left_columns(measures, SLOT_MEASURE)
        for op in CALC_OPS:
            head = base + s(coordinate(SLOT_CALC_OP, op)) + self.type_score(A_PERCENT if op == OVER else A_NUMBER)
            # one column, two lines or two sets of lines: the value differs
            for held, other in groups:
                frames.append(_Frame(head + aggregate + conditions(2), (plain, held, other), lambda options, o=op: _contrast_agg(table, index, options, o)))
                frames.append(_Frame(head + conditions(2), (shown_numbers, held, other), lambda options, o=op: _contrast_show(table, options, o)))
                frames.append(_Frame(head + aggregate + conditions(3), (plain, held, other, selectors[0]), lambda options, o=op: _contrast_agg(table, index, options, o)))
            for count, chosen in enumerate(read_lines, 1):
                # one line, two columns
                frames.append(_Frame(head + conditions(count), (left_shown, shown_numbers, chosen), lambda options, o=op: _columns_on_a_line(table, options, o)))
            # one set of lines, two measures
            frames.append(_Frame(head + aggregate + conditions(0), (left_measures, plain), lambda options, o=op: _two_measures(table, options, o)))
            for count, chosen in enumerate(selectors, 1):
                frames.append(_Frame(head + aggregate + conditions(count), (left_measures, plain, chosen), lambda options, o=op: _two_measures(table, options, o)))
        # a share: part over whole, and a over a plus b on one line
        share = base + s(coordinate(SLOT_CALC_OP, OVER)) + self.type_score(A_PERCENT) + conditions(1)
        parts = selectors[0]
        if self.positive(SLOT_CALC_LEFT):
            parts = sorted((_Option(option.score + sum(self.s(coordinate(SLOT_CALC_LEFT, name)) for name in option.payload[0].names), option.payload) for option in parts), key=lambda option: -option.score)
        frames.append(_Frame(share + aggregate, (plain, parts), lambda options: _part_of_whole(table, options)))
        frames.append(_Frame(share + s(coordinate(SLOT_CALC_OP, PLUS)), (left_shown, shown_numbers, read_lines[0]), lambda options: _share_on_a_line(table, options)))
        return frames

    def cross_table_frames(self) -> list[_Frame]:
        """Two tables: two blocks of one tab or two tabs, the same line in each. Only for tables the reading itself names."""
        s = self.s
        form = s(coordinate(SLOT_FORM, CALC))
        named = [table for table in self.decoder.catalog.tables if self.reading.score(coordinate(SLOT_TABLE, table.id)) > 0]
        if coordinate(SLOT_FORM, CALC) not in self.reading.scores or len(named) < 2:
            return []
        frames: list[_Frame] = []
        for first in named:
            for second in named:
                if first.id == second.id:
                    continue
                a_columns = self.column_options(first, self.decoder.indexes[first.id].number_columns, SLOT_SHOWN)
                b_columns = self.column_options(second, self.decoder.indexes[second.id].number_columns, SLOT_SHOWN)
                a_atoms = self.per_column(self.atoms_of(first, self.decoder.indexes[first.id]), self.policy.rank_values)
                b_atoms = self.per_column(self.atoms_of(second, self.decoder.indexes[second.id]), self.policy.rank_values)
                base = form + s(coordinate(SLOT_TABLE, first.id)) + s(coordinate(SLOT_TABLE, second.id))
                for op in CALC_OPS:
                    frames.append(_Frame(base + s(coordinate(SLOT_CALC_OP, op)), (a_columns, a_atoms, b_columns, b_atoms),
                                         lambda options, a=first, b=second, o=op: Calc(Show(a.id, (options[0],), (options[1].condition,)), o, Show(b.id, (options[2],), (options[3].condition,)))))
        return frames


def _number_or_none(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _occurring(index: TableIndex, table: CatalogTable, atoms: Sequence[Atom], any_line: bool = False) -> bool:
    """Conditions on distinct columns that sit together on at least one line: a data line, unless a condition addresses printed lines or the form reads any line, as a show does."""
    if len({atom.group for atom in atoms}) != len(atoms):
        return False
    mask = table.all_mask if any_line or any(isinstance(atom.condition, OnLine) for atom in atoms) else table.data_mask
    for atom in atoms:
        mask &= index.mask(atom.condition)
        if not mask:
            return False
    return True


def _keeps_lines(index: TableIndex, table: CatalogTable, atoms: Sequence[Atom]) -> bool:
    mask = table.all_mask
    for atom in atoms:
        mask &= index.mask(atom.condition)
    return bool(mask)


def _contrast_agg(table, index, options, op):
    measure, left, right, *shared = options
    if left is right or (op == PLUS and repr(left.condition) > repr(right.condition)):
        return None
    atoms = shared[0] if shared else ()
    if any(atom.group == left.group for atom in atoms) or (atoms and not (_keeps_lines(index, table, (left, *atoms)) and _keeps_lines(index, table, (right, *atoms)))):
        return None
    with_shared = tuple(atom.condition for atom in atoms)
    return Calc(Agg(SUM, measure, table.id, with_shared + (left.condition,)), op, Agg(SUM, measure, table.id, with_shared + (right.condition,)))


def _contrast_show(table, options, op):
    column, left, right = options
    if left is right or (op == PLUS and repr(left.condition) > repr(right.condition)):
        return None
    return Calc(Show(table.id, (column,), (left.condition,)), op, Show(table.id, (column,), (right.condition,)))


def _columns_on_a_line(table, options, op):
    a, b, atoms = options
    if a == b or (op == PLUS and a > b):
        return None
    where = tuple(atom.condition for atom in atoms)
    return Calc(Show(table.id, (a,), where), op, Show(table.id, (b,), where))


def _two_measures(table, options, op):
    a, b, *chosen = options
    if a == b or (op == PLUS and repr(a) > repr(b)):
        return None
    where = tuple(atom.condition for atom in chosen[0]) if chosen else ()
    return Calc(Agg(SUM, a, table.id, where), op, Agg(SUM, b, table.id, where))


def _part_of_whole(table, options):
    measure, atoms = options
    return Calc(Agg(SUM, measure, table.id, (atoms[0].condition,)), OVER, Agg(SUM, measure, table.id))


def _share_on_a_line(table, options):
    a, b, atoms = options
    if a == b:
        return None
    where = (atoms[0].condition,)
    return Calc(Show(table.id, (a,), where), OVER, Calc(Show(table.id, (b,), where), PLUS, Show(table.id, (a,), where)))


__all__ = ["Decoded", "Decoder", "POLICY", "Policy"]
