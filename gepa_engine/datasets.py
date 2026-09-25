"""Case rules and sealing for an optimization dataset.

Cases are data the evaluator interprets. Preparing them enforces what every experiment needs
(unique IDs, an input, train/val/test membership, no credentials, and no duplicate or related
cases across partitions without notice) and collects every problem at once, so a person can
fix them in one pass; evaluator-specific rules (``expected`` among the options...) come from
the adapter through ``check_case``. Sealing binds the approved cases to the artifact, adapter
and evaluator: the ``ds-`` id is the hash of that binding.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

DRAFT_FORMAT = "gepa-draft-v1"
SEALED_FORMAT = "gepa-dataset-v2"
# What an approval binds: changing any of these is a new version that needs its own approval.
BINDING_KEYS = ("cases", "artifactSha256", "objective", "adapter", "evaluator", "fixedContract", "mutableSurface", "adapterCodeSha256")
# Bound only when a draft has them, so versions sealed without them keep their id: a draft prepared from a template seals the template's version.
OPTIONAL_BINDING_KEYS = ("template",)
SPLITS = ("train", "val", "test")
PREVIEW_SPLITS = ("train", "val")  # the search partitions; the reserved test never enters a preview
SPLIT_ALIASES = {"train": "train", "entrenamiento": "train", "val": "val", "validation": "val", "validacion": "val",
                 "test": "test", "prueba": "test"}
SPLIT_PATTERN = ("train", "val", "train", "test", "train")  # dealt in turn: 60 % train, 20 % val, 20 % test
MIN_FOR_SPLIT = 4  # the pattern reaches test at the fourth family
DEFAULT_SEED = 42
GENERIC_FIELDS = frozenset({"id", "input", "split", "source", "group", "notes"})  # what every case may carry, whatever the adapter
MAX_CASES = 10_000
MAX_ID = 200
MAX_LISTED = 5  # case IDs quoted in one issue
MAX_RELATED_REPORTED = 20
RELATED_SIMILARITY = 0.8  # share of distinct words two inputs have in common to count as near copies (Jaccard)
RELATED_MIN_WORDS = 4  # shorter inputs are only compared for equality
MAX_RELATED_COMPARISONS = 2_000_000  # bounds time and memory when thousands of inputs share most of their words
MAX_RELATED_PAIRS = 20_000
# A key names a credential when it ends with one of these (``OPENROUTER_API_KEY``, ``clientSecret``),
# not when it merely contains the word (``authorizationStatus``, ``password_reset``).
SECRET_SUFFIXES = ("apikey", "authorization", "password", "passwd", "secret", "credential", "credentials", "accesstoken", "refreshtoken", "bearertoken")

CaseCheck = Callable[[Mapping[str, Any]], "str | None"]
Family = list[dict[str, Any]]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def find_secret_key(value: Any) -> str | None:
    """Name of the first key that looks like a credential, anywhere in ``value``."""
    if isinstance(value, dict):
        for key, child in value.items():
            compact = "".join(ch for ch in str(key).lower() if ch.isalnum())
            if compact.endswith(SECRET_SUFFIXES):
                return str(key)
            found = find_secret_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_secret_key(child)
            if found:
                return found
    return None


def fold(text: str) -> str:
    """Case-, accent- and whitespace-insensitive form used to recognize the same text."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).split())


def normalized_input(value: Any) -> str:
    """Text used to detect the same case twice."""
    return fold(value if isinstance(value, str) else canonical_json(value))


def _place(source: Mapping[str, Any]) -> str:
    """The file and sheet, the pasted table or the inline list a case came from."""
    if source.get("kind") == "file":
        return source["file"] + (f" · hoja {source['sheet']}" if source.get("sheet") else "")
    return "tabla pegada" if source.get("kind") == "pasted" else "casos aportados"


def where(case: Mapping[str, Any]) -> str:
    """Where a person finds the case: its place and its row (as a spreadsheet shows it) or position."""
    source = case.get("source") or {}
    position = f"fila {source['row']}" if "row" in source else f"caso {source.get('index', '?')}"
    return f"{_place(source)}, {position}"


def provenance_text(case: Mapping[str, Any]) -> str:
    """Where a case came from plus what was declared about it, for listing cases to review."""
    source = case.get("source") or {}
    return " · ".join([where(case), *(source[key] for key in ("origin", "declared") if source.get(key))])


def _issue(severity: str, code: str, message: str, cases: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "cases": list(cases)}


@dataclass(frozen=True)
class Prepared:
    """Normalized cases with their partitions, every problem found, and the partition rule used."""

    cases: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    split: dict[str, Any]

    @property
    def ready(self) -> bool:
        return not any(item["severity"] == "error" for item in self.issues)


def prepare(raw: list[dict[str, Any]], *, check_case: CaseCheck, used_fields: frozenset[str] = frozenset(), labels: tuple[str, ...] = (),
            seed: int = DEFAULT_SEED) -> Prepared:
    """Validate and normalize imported cases; nothing is stored or sent to a model here.

    ``used_fields`` are the case fields the adapter reads; any other data field is reported,
    because it cannot influence the execution or the score. ``labels`` are the expected values
    the evaluator can score, each of which validation and test should cover; the automatic split
    deals each expected value in turn. Without labels (an evaluator whose ``expected`` is not
    one of a few values), cases are dealt without looking at ``expected``. ``seed`` drives
    the split of cases that carry no partition.
    """
    issues: list[dict[str, Any]] = []
    cases = _identified([dict(item) for item in raw], issues)
    for case in cases:
        _check(case, check_case, issues)
    carriers: dict[str, list[str]] = {}
    for case in cases:
        for field in sorted(set(case) - GENERIC_FIELDS - used_fields):
            carriers.setdefault(field, []).append(case["id"])
    for field, holders in carriers.items():
        issues.append(_issue("warning", "field-ignored", f"{len(holders)} casos traen '{field}', un campo que este adaptador no usa: no influye en la ejecución ni en la puntuación.", holders[:MAX_LISTED]))
    duplicates, groups, related, complete = _kinship(cases)
    if not complete:
        issues.append(_issue("warning", "related-check-incomplete", f"Hay demasiados casos casi iguales para compararlos todos: la búsqueda se detuvo con {len(related)} pares. "
                                                                   "Si los casos salen de una plantilla, indica 'group' o 'split' para que los parecidos queden juntos."))
    split = _partition(cases, duplicates + groups + related, seed, issues, stratified=bool(labels))
    # What the evaluator reads besides the input: two cases with the same input and different values of these conflict.
    _crossings(duplicates, groups, related, issues, tuple(sorted(used_fields - {"input"})) or ("expected",))
    if not any(item["code"] in ("invalid-split", "partial-split", "too-few-for-split") for item in issues):
        missing = [name for name in SPLITS if not any(case.get("split") == name for case in cases)]
        if missing:
            issues.append(_issue("error", "missing-partition", f"Faltan casos en la partición {', '.join(missing)}: se necesitan train, val y test."))
        for label, counts in coverage(cases, labels)["expected"].items():
            absent = [split for split in ("val", "test") if not counts[split]]
            if absent:
                issues.append(_issue("warning", "label-missing-in-partition", f"La opción «{label}» no tiene casos en {' ni en '.join(absent)}: "
                                                                               "esa parte de la evaluación no puede medirla."))
    return Prepared(cases=cases, issues=issues, split=split)


def _identified(cases: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the IDs given (reporting invalid, repeated or empty ones) and derive the rest from the input, whatever the row order."""
    blank = []
    for case in cases:
        present = "id" in case
        identifier = case.pop("id", None)
        if isinstance(identifier, str) and len(identifier.strip()) <= MAX_ID:
            if identifier.strip():
                case["id"] = identifier.strip()
            else:
                blank.append(case)
        elif identifier is not None:
            issues.append(_issue("error", "invalid-id", f"{where(case)}: 'id' debe ser texto de hasta {MAX_ID} caracteres."))
        elif present:
            blank.append(case)
    for identifier, count in Counter(case["id"] for case in cases if "id" in case).items():
        if count > 1:
            issues.append(_issue("error", "duplicate-id", f"El id '{identifier}' se repite en {count} casos; cada caso necesita un id único.", [identifier]))
    taken = {case["id"] for case in cases if "id" in case}
    for case in cases:
        if "id" in case:
            continue
        base = "c-" + sha256_of(case.get("input"))[:12]
        identifier, suffix = base, 1
        while identifier in taken:
            suffix += 1
            identifier = f"{base}-{suffix}"
        case["id"] = identifier
        taken.add(identifier)
    if blank:
        issues.append(_issue("warning", "blank-id", f"{len(blank)} casos traen 'id' vacío y recibieron uno derivado de su entrada; "
                                                    f"complétalo en el origen si debe identificarlos (primero: {where(blank[0])}).", [case["id"] for case in blank][:MAX_LISTED]))
    return cases


def _check(case: dict[str, Any], check_case: CaseCheck, issues: list[dict[str, Any]]) -> None:
    """Rules every case follows, then what the evaluator needs from it."""
    def report(code: str, message: str) -> None:
        issues.append(_issue("error", code, f"{where(case)}: {message}", [case["id"]]))

    secret = find_secret_key({key: value for key, value in case.items() if key != "source"})
    if secret:
        report("secret-field", f"el campo '{secret}' parece una credencial; las credenciales solo se configuran en las conexiones.")
    value = case.get("input")
    if value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (dict, list)) and not value):
        report("empty-input", "el caso necesita un 'input' no vacío.")
        case.pop("input", None)
    if "split" in case:
        raw_split = case.pop("split")
        split = SPLIT_ALIASES.get(fold(raw_split)) if isinstance(raw_split, str) else None
        if split is None:
            report("invalid-split", f"la partición «{raw_split}» no es train, val ni test.")
        else:
            case["split"] = split
    if "group" in case:
        group = case.pop("group")
        if isinstance(group, (str, int)) and not isinstance(group, bool) and str(group).strip():
            case["group"] = str(group).strip()
        else:
            report("invalid-group", "'group' debe ser un texto que identifique casos relacionados.")
    problem = check_case(case) if "input" in case else None
    if problem:
        report("evaluator-requirement", problem)


def _tokens(value: Any) -> frozenset[str]:
    """Words of the text values of an input, without case or accents; the keys of a JSON state do not count."""
    if isinstance(value, str):
        return frozenset(re.findall(r"\w+", fold(value)))
    if isinstance(value, dict):
        return frozenset().union(*map(_tokens, value.values()))
    if isinstance(value, list):
        return frozenset().union(*map(_tokens, value))
    return frozenset() if value is None else frozenset({fold(str(value))})


def related_pairs(inputs: list[Any]) -> tuple[list[tuple[int, int]], bool]:
    """Index pairs of inputs whose word sets overlap by at least ``RELATED_SIMILARITY`` (Jaccard), and whether the search finished.

    Exact prefix filtering: with words ranked rarest first, two sets that similar share a word
    among the first ``n - ceil(t * n) + 1`` words of the larger one, so only those pairs are compared.
    The search stops after ``MAX_RELATED_COMPARISONS`` comparisons or ``MAX_RELATED_PAIRS`` pairs.
    """
    sets = [_tokens(value) for value in inputs]
    frequency = Counter(token for words in sets for token in words)
    index: dict[str, list[int]] = {}
    pairs: set[tuple[int, int]] = set()
    compared = 0
    for position in sorted(range(len(sets)), key=lambda k: (len(sets[k]), k)):
        words = sets[position]
        if len(words) < RELATED_MIN_WORDS:
            continue
        prefix = sorted(words, key=lambda token: (frequency[token], token))[:len(words) - math.ceil(RELATED_SIMILARITY * len(words)) + 1]
        candidates = sorted({other for token in prefix for other in index.get(token, ())})
        for token in prefix:
            index.setdefault(token, []).append(position)
        for other in candidates:
            compared += 1
            if compared > MAX_RELATED_COMPARISONS or len(pairs) >= MAX_RELATED_PAIRS:
                return sorted(pairs), False
            if len(words & sets[other]) / len(words | sets[other]) >= RELATED_SIMILARITY:
                pairs.add((min(position, other), max(position, other)))
    return sorted(pairs), True


def _kinship(cases: list[dict[str, Any]]) -> tuple[list[Family], list[Family], list[Family], bool]:
    """Cases that are the same (normalized input), declared related (``group``) or near copies of each other.

    The last value says whether every near copy was searched for (see :func:`related_pairs`).
    """
    by_text: dict[str, Family] = {}
    by_group: dict[str, Family] = {}
    for case in cases:
        if "input" in case:
            by_text.setdefault(normalized_input(case["input"]), []).append(case)
        if "group" in case:
            by_group.setdefault(case["group"], []).append(case)
    # Ordered by text, so a bounded search finds the same pairs whatever the order of the rows.
    distinct = [by_text[text][0] for text in sorted(by_text)]
    found, complete = related_pairs([case["input"] for case in distinct])
    row = {id(case): position for position, case in enumerate(cases)}
    related = [sorted((distinct[i], distinct[j]), key=lambda case: row[id(case)]) for i, j in found]
    return [members for members in by_text.values() if len(members) > 1], [members for members in by_group.values() if len(members) > 1], related, complete


def _names(members: Family) -> str:
    return ", ".join(f"{case['id']} ({case.get('split', 'sin partición')})" for case in members[:MAX_LISTED])


def _crossings(duplicates: list[Family], groups: list[Family], related: list[Family], issues: list[dict[str, Any]], answers: tuple[str, ...]) -> None:
    """Report the same, declared-related and nearly equal cases, and block the ones that cross partitions or disagree on the ``answers`` fields."""
    def splits(members: Family) -> set[str]:
        return {case["split"] for case in members if "split" in case}

    def ids(members: Family) -> list[str]:
        return [case["id"] for case in members][:MAX_LISTED]

    for members in duplicates:
        if len({canonical_json([case.get(field) for field in answers]) for case in members}) > 1:
            fields = " o ".join(f"'{field}'" for field in answers)
            issues.append(_issue("error", "conflicting-duplicate", f"Los casos {_names(members)} tienen la misma entrada y distinto {fields}; corrige o elimina el erróneo.", ids(members)))
        elif len(splits(members)) > 1:
            issues.append(_issue("error", "duplicate-across-partitions", f"Los casos {_names(members)} tienen la misma entrada (sin distinguir mayúsculas, tildes ni espacios) en particiones distintas.", ids(members)))
        else:
            issues.append(_issue("warning", "duplicate-case", f"Los casos {_names(members)} repiten la misma entrada: cuentan varias veces en la puntuación.", ids(members)))
    for members in groups:
        if len(splits(members)) > 1:
            issues.append(_issue("error", "group-across-partitions", f"El grupo «{members[0]['group']}» tiene casos en particiones distintas: {_names(members)}.", ids(members)))
    crossing = [members for members in related if len(splits(members)) > 1]
    for members in crossing[:MAX_RELATED_REPORTED]:
        issues.append(_issue("warning", "related-across-partitions", f"Los casos {_names(members)} son casi iguales y están en particiones distintas: "
                                                                    "la validación o la prueba reservada pueden sobrestimar el resultado.", ids(members)))
    if len(crossing) > MAX_RELATED_REPORTED:
        issues.append(_issue("warning", "related-across-partitions", f"Y {len(crossing) - MAX_RELATED_REPORTED} pares más de casos casi iguales en particiones distintas."))


def _families(cases: list[dict[str, Any]], linked: list[Family]) -> list[Family]:
    """Cases joined, directly or through others, by being the same, in one group or near copies."""
    parent = list(range(len(cases)))
    position = {id(case): index for index, case in enumerate(cases)}

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for members in linked:
        first = root(position[id(members[0])])
        for case in members[1:]:
            parent[root(position[id(case)])] = first
    families: dict[int, Family] = {}
    for index, case in enumerate(cases):
        families.setdefault(root(index), []).append(case)
    return list(families.values())


def _stratum(case: Mapping[str, Any], stratified: bool) -> str:
    """The group a case is dealt or sampled with: its expected value, or one group for all when the evaluator has no labels."""
    return canonical_json(case.get("expected")) if stratified else ""


def _partition(cases: list[dict[str, Any]], linked: list[Family], seed: int, issues: list[dict[str, Any]], *, stratified: bool = True) -> dict[str, Any]:
    """Keep explicit partitions, or deal related-case families in turn (60/20/20) with ``seed``, by expected value when ``stratified``.

    Families are ordered by their smallest case ID before the seeded shuffle, so the result depends
    on the cases and the seed, not on the order of the rows.
    """
    with_split = [case for case in cases if "split" in case]
    if with_split:
        if len(with_split) != len(cases):
            without = [case["id"] for case in cases if "split" not in case]
            issues.append(_issue("error", "partial-split", f"{len(without)} casos no indican partición; indícala en todos o en ninguno.", without[:MAX_LISTED]))
        return {"mode": "explicit"}
    rule = {"mode": "seeded", "seed": seed, "proportions": {"train": 0.6, "val": 0.2, "test": 0.2},
            "keptTogether": "Los casos con la misma entrada, el mismo 'group' o entradas casi iguales van a la misma partición."}
    families = _families(cases, linked)
    if len(families) < MIN_FOR_SPLIT:
        issues.append(_issue("error", "too-few-for-split", f"La división automática necesita al menos {MIN_FOR_SPLIT} casos distintos; "
                                                           f"hay {len(families)}. Añade casos o indica 'split' en cada uno."))
        return rule
    by_label: dict[str, list[Family]] = {}
    for family in sorted(families, key=lambda members: min(case["id"] for case in members)):
        by_label.setdefault(min(_stratum(case, stratified) for case in family), []).append(family)
    rng = random.Random(seed)
    turn = 0
    for label in sorted(by_label):
        members = by_label[label]
        rng.shuffle(members)
        for family in members:
            for case in family:
                case["split"] = SPLIT_PATTERN[turn % len(SPLIT_PATTERN)]
            turn += 1
    return rule


def _source_label(source: Mapping[str, Any]) -> str:
    note = None if source.get("kind") == "file" else source.get("origin") or source.get("declared")
    return f"{_place(source)} · {note}" if note else _place(source)


def coverage(cases: list[dict[str, Any]], labels: tuple[str, ...] = ()) -> dict[str, Any]:
    """How the cases spread over partitions, expected values (every label, even without cases) and sources.

    Without labels there is no per-value coverage: each case's ``expected`` is its own set of checks.
    Two files with the same name but different content are two sources, told apart by their SHA-256.
    """
    expected = {label: dict.fromkeys(SPLITS, 0) for label in labels}
    for case in cases:
        if labels and "expected" in case and case.get("split") in SPLITS:
            label = case["expected"] if isinstance(case["expected"], str) else canonical_json(case["expected"])
            expected.setdefault(label, dict.fromkeys(SPLITS, 0))[case["split"]] += 1
    sources = Counter((_source_label(case.get("source") or {}), (case.get("source") or {}).get("fileSha256")) for case in cases)
    return {"total": len(cases), "expected": expected,
            "sources": [{"source": label, **({"fileSha256": digest} if digest else {}), "cases": count} for (label, digest), count in sources.items()]}


def seal_sha256(record: Mapping[str, Any]) -> str:
    """Hash of the approved cases bound to artifact, adapter, evaluator, their code and the template they came from; ``ds-`` plus its start is the id."""
    return sha256_of({key: record[key] for key in (*BINDING_KEYS, *(key for key in OPTIONAL_BINDING_KEYS if key in record))})


def partitions(cases: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {split: [case["id"] for case in cases if case.get("split") == split] for split in SPLITS}


def _preview_pool(cases: list[dict[str, Any]], stratified: bool) -> dict[str, list[dict[str, Any]]]:
    """Training and validation cases by expected value (or all together), in ID order; the reserved test partition never enters a preview."""
    by_label: dict[str, list[dict[str, Any]]] = {}
    for case in sorted((case for case in cases if case["split"] in PREVIEW_SPLITS), key=lambda case: case["id"]):
        by_label.setdefault(_stratum(case, stratified), []).append(case)
    return by_label


def preview_minimum(cases: list[dict[str, Any]], *, stratified: bool = True) -> int:
    """Smallest representative preview: both partitions and, when ``stratified``, one case per expected value found in train and val."""
    pool = _preview_pool(cases, stratified)
    return max(len(pool), len({case["split"] for members in pool.values() for case in members}))


def preview_sample(cases: list[dict[str, Any]], size: int, seed: int = 0, *, stratified: bool = True) -> list[dict[str, Any]]:
    """Up to ``size`` training and validation cases: expected values in turn (when ``stratified``), alternating partitions.

    With ``size`` at least :func:`preview_minimum`, every expected value and both partitions show
    up. The choice depends on the cases and the seed, not on their order.
    """
    rng = random.Random(seed)
    pools = []
    for _, members in sorted(_preview_pool(cases, stratified).items()):
        rng.shuffle(members)
        pools.append({split: [case for case in members if case["split"] == split] for split in PREVIEW_SPLITS})
    # First turn: one case per expected value, starting alternately in train and val...
    last = [next(split for split in (PREVIEW_SPLITS if index % 2 == 0 else PREVIEW_SPLITS[::-1]) if pool[split]) for index, pool in enumerate(pools)]
    # ...and a partition nobody drew goes to the first value that has it: from two values on, the other partition keeps a case.
    for missing in [split for split in PREVIEW_SPLITS if split not in last and any(pool[split] for pool in pools)]:
        last[next(index for index, pool in enumerate(pools) if pool[missing])] = missing
    chosen = [pool[split].pop(0) for pool, split in zip(pools, last)][:size]
    while len(chosen) < size and any(pool[split] for pool in pools for split in PREVIEW_SPLITS):
        for index, pool in enumerate(pools):
            split = next((split for split in sorted(PREVIEW_SPLITS, key=lambda split: split == last[index]) if pool[split]), None)
            if split is not None and len(chosen) < size:
                chosen.append(pool[split].pop(0))
                last[index] = split
    return chosen
