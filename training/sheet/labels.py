"""Line labels on train-family tabs, read by hand (an LLM's reading, styles and formula marks in view, no prediction in view). Tab index -> role codes, ranges inclusive: H header, S band, T printed total, D record, C title, N note.
The tabs are the ones listed in phase1_label_tabs_T.json under evals/enron/design (outside this repository), each a train-family workbook by design/corpora_family_map.json; lines not listed are not scored or trained on.
The LLM markup in evals/enron/annotations.jsonl covers only the studied workbooks, which are quarantined from training, so it cannot serve here. No cell text is held in this file."""

FORM = dict(C="1,2,4,6,8,12-14", H="15,16")
LABELS_T = {
    0: dict(FORM, T="24,30,42", D="26-28", S="35", N="48,49,51,56-59"),
    1: dict(H="1-3,26-28", S="4,29", D="6-21"),
    2: dict(C="1,2", H="5,6", D="7-26"),
    3: dict(D="3-24"),
    4: dict(C="2", H="4", D="5-26"),
    5: dict(H="1,2", D="3-11,16,17,21,22", T="13,19,24,26"),
    6: dict(H="1,2", D="4-20"),
    7: dict(H="2,3", D="4,5,7,9,11-18,20,22-30"),
    8: dict(C="1", H="3", D="4-25"),
    9: dict(C="1-3", H="5,6", D="8-26"),
    10: dict(FORM, D="18-20", T="24,30,40", S="33", N="46,47,49,54-57"),
    11: dict(C="1", H="2", D="3,4,6,8,9,11,13,15-17,19,21,23,24", T="5,7,10,12,14,18,20,22"),
    12: dict(H="2-5", S="6,9,10,15", D="7,11-13,16-18", N="24"),
    13: dict(H="1", D="2-24"),
    14: dict(C="1,2", H="5-9", D="10-17,19-26", N="29"),
    15: dict(H="5", D="6-28"),
    16: dict(N="2-17"),
    17: dict(N="2-17"),
    18: dict(C="1,2", S="4,14", D="5,7,9,10,15,17,18"),
    19: dict(C="2", H="4", D="5-26"),
    20: dict(C="1-3,5,8,9", S="11,18,25", D="12-16,19-23,26-30"),
    21: dict(C="1", H="3"),
    22: dict(D="1-24"),
    23: dict(H="1", D="3-12,14-26"),
    25: dict(C="1,2", H="4", D="5-25"),
    26: dict(S="1", D="2-5,7-10,12-15,17"),
    27: dict(FORM, T="23,28,40", S="33", N="46,47,49,54-57"),
    28: dict(H="1", S="3,8,11,14,21,27,30", D="4-6,9,12,15-19,22-25,28,31"),
    29: dict(H="1", D="2-24"),
    30: dict(H="1", D="2-24"),
    31: dict(C="1", H="3", D="4-25"),
    32: dict(H="1", D="2-14,16-24", T="15"),
    33: dict(D="1-14"),
}


def expand(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def rows_of(labels: dict[str, str]) -> dict[int, str]:
    got: dict[int, str] = {}
    for code, text in labels.items():
        for r in expand(text):
            got[r] = code
    return got
