"""Deterministic domain-role evidence over request-local table structure."""
from __future__ import annotations

from dataclasses import dataclass
import re

from engine.domain_profiles import PROFILES, RoleDefinition


_WEAK_TABLE_WORDS = frozenset({"data", "records", "record", "sheet", "table", "list", "details", "values"})
_FIELD_FILLER_WORDS = frozenset({
    "a", "an", "and", "are", "at", "by", "for", "from", "in", "is", "of", "on", "or",
    "the", "to", "was", "were", "what", "when", "which", "who", "your",
})
_WEAK_FIELD_ALIASES = frozenset({
    "address", "amount", "city", "code", "country", "currency", "date", "description",
    "email", "id", "name", "phone", "price", "status", "title", "total", "value",
})


def _tokens(value) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _canonical(value) -> str:
    return "_".join(_tokens(value))


def _field_tokens(value) -> frozenset[str]:
    normalized = []
    for token in _tokens(value):
        if token in _FIELD_FILLER_WORDS:
            continue
        if token in {"number", "no", "identifier"}:
            token = "id"
        elif token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
            token = token[:-1]
        normalized.append(token)
    return frozenset(normalized)


def _matching_field(alias: str, columns: dict[str, frozenset[str]]) -> str | None:
    canonical = _canonical(alias)
    if canonical in columns:
        return canonical
    wanted = _field_tokens(alias)
    if len(wanted) < 2:
        return None
    return next((column for column, tokens in columns.items() if wanted == tokens), None)


@dataclass(frozen=True)
class RoleEvidence:
    profile: str
    role: str
    table: str
    score: float
    evidence: tuple[str, ...]
    schema_org_classes: tuple[str, ...]


@dataclass(frozen=True)
class ProfileEvidence:
    profile: str
    score: float
    roles: tuple[str, ...]
    tables: tuple[str, ...]


def _role_score(table: dict, role: RoleDefinition) -> tuple[float, tuple[str, ...]]:
    table_name = _canonical(table.get("name", ""))
    table_words = set(_tokens(table_name))
    columns = {
        _canonical(column): _field_tokens(column) for column in (table.get("columns") or ())
    }
    evidence = []
    alias_score = 0.0
    for alias in role.aliases:
        canonical = _canonical(alias)
        alias_words = set(_tokens(canonical))
        if table_name == canonical:
            alias_score = max(alias_score, 0.92)
            evidence.append(f"table-exact:{canonical}")
        elif alias_words and alias_words <= table_words and not alias_words <= _WEAK_TABLE_WORDS:
            alias_score = max(alias_score, 0.80)
            evidence.append(f"table-phrase:{canonical}")
    matched_groups = 0
    strong_groups = 0
    for group in role.column_groups:
        match = None
        for alias in group:
            column = _matching_field(alias, columns)
            if column is not None:
                match = (_canonical(alias), column)
                break
        if match is not None:
            matched_groups += 1
            alias, column = match
            if alias not in _WEAK_FIELD_ALIASES:
                strong_groups += 1
            evidence.append(f"column:{column}~{alias}")
    column_score = min(0.18, matched_groups * 0.06)
    if alias_score == 0.0:
        role_words = set(_tokens(role.name))
        if role_words and role_words <= table_words and matched_groups >= 2:
            alias_score = 0.68
            evidence.append(f"role-name:{role.name}")
        elif (role.structural_min_groups and matched_groups >= role.structural_min_groups
              and strong_groups >= 1):
            alias_score = 0.92
            evidence.append(f"column-structure:{matched_groups}/{role.structural_min_groups}")
    score = min(1.0, alias_score + column_score)
    return score, tuple(dict.fromkeys(evidence))


def detect_roles(tables) -> tuple[RoleEvidence, ...]:
    found = []
    for table in tables or ():
        table_name = str(table.get("name", ""))
        if not table_name.strip() or not table.get("columns"):
            continue
        table_found = []
        for profile_name in sorted(PROFILES):
            profile = PROFILES[profile_name]
            for role in profile.roles:
                score, evidence = _role_score(table, role)
                if score >= 0.80:
                    table_found.append(RoleEvidence(
                        profile_name, role.name, table_name, round(score, 4), evidence,
                        role.schema_org_classes,
                    ))
        if any(any(item.startswith("table-exact:") for item in role.evidence)
               for role in table_found):
            table_found = [
                role for role in table_found
                if any(item.startswith("table-exact:") for item in role.evidence)
            ]
        found.extend(table_found)
    return tuple(sorted(found, key=lambda item: (-item.score, item.profile, item.role, item.table)))


def detect_profiles(tables) -> tuple[ProfileEvidence, ...]:
    roles = detect_roles(tables)
    found = []
    for profile_name in sorted(PROFILES):
        relevant = [item for item in roles if item.profile == profile_name]
        if not relevant:
            continue
        definitions = {role.name: role for role in PROFILES[profile_name].roles}
        role_names = {item.role for item in relevant}
        distinctive = any(definitions[item.role].distinctive and item.score >= 0.90 for item in relevant)
        if profile_name != "common_party_location" and not distinctive and len(role_names) < 2:
            continue
        score = sum(max(item.score for item in relevant if item.role == role) for role in role_names)
        score /= len(role_names)
        found.append(ProfileEvidence(
            profile_name, round(score, 4), tuple(sorted(role_names)),
            tuple(sorted({item.table for item in relevant})),
        ))
    return tuple(sorted(found, key=lambda item: (-item.score, item.profile)))
