"""Independent recomputation of 20 sampled golds (own pandas code; the pipeline's programs were not read).
usage: python -I -X utf8 work/independent_check/recompute.py   -> work/independent_check/results.json"""
import json
import os
import re

import warnings  # noqa: E402

warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
W = os.path.join(ROOT, "workbooks")
DASH = chr(0x2013)
DOTS = re.compile("[." + chr(0x2026) + "]+$")


def sheet(name, tab):
    return pd.read_excel(os.path.join(W, name), sheet_name=tab, header=None)


def label(v):
    return DOTS.sub("", str(v)).strip()


def c1():
    d = sheet("census_state_to_state_migration_2023.xlsx", "tool_input")
    flows = d.iloc[1:, [0, 1, 2]].copy()
    flows.columns = ["origin", "dest", "n"]
    flows["n"] = pd.to_numeric(flows["n"], errors="coerce")
    states = set(flows["dest"].dropna())
    out = flows[flows["origin"].isin(states) & (flows["origin"] != flows["dest"])].groupby("origin")["n"].sum()
    summary = d.iloc[1:, [5, 8]].dropna()
    summary.columns = ["state", "out"]
    summary = summary[summary["state"].isin(states)]
    a, b = out.idxmax(), summary.loc[pd.to_numeric(summary["out"]).idxmax(), "state"]
    return a if a == b else f"DISAGREE {a} / {b}"


def c2():
    d = sheet("va_geographic_distribution_of_expenditures_fy2023.xlsx", "HI")
    v = {label(r[0]): r[10] for r in d.itertuples(index=False)}
    return "HAWAII" if v["HAWAII"] > v["MAUI"] else "MAUI"


def c3():
    d = sheet("irs_data_book_gross_collections_by_state_fy2023.xlsx", "Table 5")
    v = {label(r[0]): r[1] for r in d.itertuples(index=False)}
    return "Texas" if v["Texas"] > v["New York"] else "New York"


def c4():
    d = sheet("census_state_to_state_migration_2023.xlsx", "tool_input")
    m = d[(d[0] == "California") & (d[1] == "Texas")]
    assert len(m) == 1
    return int(m.iloc[0, 2])


def c5():
    d = sheet("ers_food_availability_red_meat_poultry_fish.xlsx", "Boneless")
    m = d[pd.to_numeric(d[0], errors="coerce") == 2019]
    assert len(m) == 1
    return float(m.iloc[0, 11])


def c6():
    d = pd.read_csv(os.path.join(W, "or_weekly_reportable_diseases.csv"))
    n = d.groupby("MMWRyear").size().sort_values(ascending=False)
    assert n.iloc[0] > n.iloc[1], "tie"
    return str(int(n.index[0]))


def c7():
    d = sheet("cms_marketplace_oep_state_level_2024.xlsx", "(3) by Switching Status")
    body = d[(d[2] == "HC.gov") & (d[0] != "Total")]
    total = d[(d[2] == "HC.gov") & (d[0] == "Total")]
    s = int(pd.to_numeric(body[4]).sum())
    assert s == int(total.iloc[0, 4]), (s, total.iloc[0, 4])
    return s


def c8():
    d = sheet("nchs_hus2022_hypertension.xlsx", "VI")
    groups = ("20" + DASH + "44", "45" + DASH + "64", "65 and older")
    v = {label(r[0]): r[1] for r in d.itertuples(index=False) if label(r[0]) in groups}
    assert len(v) == 3
    return min(v, key=v.get)


def c9():
    d = sheet("census_apportionment_2020.xlsx", "Table 1")
    ch = pd.to_numeric(d[3], errors="coerce")
    got = [label(x) for x in d[0][ch >= 2]]
    return got[0] if len(got) == 1 else got


def c10():
    d = pd.read_csv(os.path.join(W, "seattle_fremont_bridge_bicycle_counter.csv"))
    return int(d[d.columns[1]].isna().sum())


def c11():
    d = sheet("cms_marketplace_oep_state_level_2024.xlsx", "(10) BHP")
    body = d[d[0].isin(["Minnesota", "New York"])]
    assert len(body) == 2
    return int(pd.to_numeric(body[13]).sum())


def c12():
    d = sheet("fhwa_public_road_length_2023.xlsx", "A")
    rows = d.iloc[14:]
    names = rows[0].map(label)
    keep = rows[~names.isin(["District of Columbia", "Puerto Rico", "U.S. Total", "Grand Total", "nan"])]
    tot = pd.to_numeric(keep[17], errors="coerce")
    assert tot.notna().sum() == 50, tot.notna().sum()
    return int((tot > 100000).sum())


def c13():
    d = sheet("eia_epm_average_price_by_sector_and_state.xlsx", "Table_5_06_A")
    div = ["New England", "Middle Atlantic", "East North Central", "West North Central", "South Atlantic",
           "East South Central", "West South Central", "Mountain", "Pacific Contiguous", "Pacific Noncontiguous"]
    body = d[d[0].map(label).isin(div)]
    assert len(body) == 10
    top = body.assign(p=pd.to_numeric(body[9])).sort_values("p", ascending=False).head(4)
    assert top["p"].iloc[2] > top["p"].iloc[3]
    return [label(x) for x in top[0].head(3)]


def c14():
    d = sheet("census_school_system_finances_2022.xlsx", "10")
    rows = d.iloc[10:71]
    names = rows[0].map(label)
    body = rows[names.ne("nan") & names.ne("United States")]
    tot = pd.to_numeric(body[2], errors="coerce")
    assert tot.notna().sum() == 51, tot.notna().sum()
    return int((tot > 10_000_000).sum())


def c15():
    d = sheet("va_geographic_distribution_of_expenditures_fy2023.xlsx", "State Level Expenditures")
    v = {label(r[0]): r[10] for r in d.itertuples(index=False)}
    return "yes" if v["Florida"] > v["California"] else "no"


def c16():
    d = sheet("hrsa_organ_donation_transplantation.xlsx", "Donor Demographics")
    m = d[(d[1] == "Living Donor") & (d[2] == "Uninsured")]
    assert len(m) == 1
    return int(str(m.iloc[0, 3]).strip())


def c17():
    d = sheet("macpac_medicaid_spending_by_state_fy2024.xlsx", "Exhibit 16")
    v = {label(r[0]): r[2] for r in d.itertuples(index=False)}
    return "Florida" if v["Florida"] > v["Ohio"] else "Ohio"


def c18():
    d = sheet("census_voting_registration_by_state_2020.xlsx", "Table 4a")
    rows = d.iloc[7:58]
    pct = pd.to_numeric(rows[11], errors="coerce")
    assert pct.notna().sum() == 51, pct.notna().sum()
    return int((pct >= 70).sum())


def c19():
    d = sheet("hud_pit_veteran_counts_by_state.xlsx", "2024")
    rows = d.iloc[1:]
    rows = rows[rows[0].astype(str).str.fullmatch("[A-Z]{2}")]
    top = rows.assign(u=pd.to_numeric(rows[7], errors="coerce")).sort_values("u", ascending=False).head(4)
    assert top["u"].iloc[2] > top["u"].iloc[3]
    return list(top[0].head(3))


def c20():
    d = sheet("fhwa_public_road_length_2023.xlsx", "A")
    v = {label(r[0]): r[9] for r in d.itertuples(index=False)}
    return "New York" if v["New York"] > v["Florida"] else "Florida"


def same(answer_type, mine, gold):
    if answer_type == "number":
        a, b = float(mine), float(gold)
        return a == b or abs(a - b) <= 1e-6 * max(abs(a), abs(b))
    if answer_type == "list":
        return isinstance(mine, list) and sorted(str(x).casefold() for x in mine) == sorted(str(x).casefold() for x in gold)
    return not isinstance(mine, list) and str(mine).strip().casefold() == str(gold).strip().casefold()


sample = json.load(open(os.path.join(ROOT, "work", "independent_check", "sample.json"), encoding="utf-8"))["sample"]
rows = {}
for line in open(os.path.join(ROOT, "questions.jsonl"), encoding="utf-8"):
    r = json.loads(line)
    rows[r["candidate_id"]] = r
checks = [c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14, c15, c16, c17, c18, c19, c20]
out = []
for cid, fn in zip(sample, checks):
    r = rows[cid]
    try:
        mine = fn()
        ok = same(r["answer_type"], mine, r["gold"])
    except Exception as exc:  # noqa: BLE001
        mine, ok = f"ERROR {type(exc).__name__}: {exc}", False
    out.append({"candidate_id": cid, "id": r["id"], "answer_type": r["answer_type"], "gold": r["gold"], "recomputed": mine,
                "gold_confirmed": ok})
    print(("ok  " if ok else "DIFF"), cid, "| gold", json.dumps(r["gold"], ensure_ascii=False), "| mine", json.dumps(mine, ensure_ascii=False))
with open(os.path.join(ROOT, "work", "independent_check", "results.json"), "w", encoding="utf-8", newline="\n") as f:
    json.dump(out, f, indent=1, ensure_ascii=False)
print(sum(o["gold_confirmed"] for o in out), "of", len(out), "golds recomputed to the same value")
