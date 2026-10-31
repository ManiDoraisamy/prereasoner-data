"""Workbooks shaped like the sheets people actually upload: exports with their headers as the exporting tool writes them
(abbreviations, units, near-duplicates, a typo), codes in place of words, blanks, money as text, and report tabs beside
the data. Every customer, id and amount is invented; every workbook is seeded, so the gold (regress/messy_cases.py) is
fixed. "subscriptions" and "keywords" follow the shape of the owner's two sheets; none of this data trains anything.
"""
import random
from datetime import date, datetime, timedelta
from functools import lru_cache


def _pick(rng, weighted):
    values, weights = zip(*weighted)
    return rng.choices(values, weights)[0]


def _tab(name, columns, rows):
    return {"name": name, "columns": list(columns), "rows": [["" if cell is None else str(cell) for cell in row]
                                                             for row in rows]}


def _names(rng, count):
    first = ["Asha", "Ben", "Chen", "Dara", "Elif", "Farid", "Gita", "Hugo", "Ines", "Jonas", "Kira", "Liam", "Mina",
             "Noor", "Omar", "Priya", "Quinn", "Rosa", "Sven", "Tara", "Uma", "Viktor", "Wen", "Yara", "Zane"]
    last = ["Abbott", "Bauer", "Costa", "Dube", "Evans", "Fischer", "Gupta", "Haddad", "Ito", "Jensen", "Kumar", "Lopez",
            "Moreau", "Nair", "Okafor", "Patel", "Quist", "Rossi", "Silva", "Tanaka", "Ueda", "Vega", "Weber", "Xu", "Young"]
    names = sorted({f"{a} {b}" for a in first for b in last})
    return rng.sample(names, count)


_BASE = ["id", "Customer ID", "Customer Email", "Domain", "Plan", "Product", "Currency", "Cancellation Reason",
         "Cancellation Feedback", "Amount", "Subscription", "Status", "Created (UTC)", "Year", "Start Date (UTC)",
         "Current Period Start (UTC)", "Current Period End (UTC)", "Canceled At (UTC)", "Cancel At Period End"]
_NT_EXTRA = ["Industry (metadata)", "duration (metadata)", "socialmedia (metadata)", "ecommerce (metadata)",
             "website (metadata)", "state (metadata)", "country (metadata)", "subcategory (metadata)",
             "subsector (metadata)", "sector (metadata)", "industry (metadata)", "userid (metadata)",
             "categort (metadata)", "businss (metadata)", "category (metadata)", "business (metadata)",
             "Business (metadata)", "email (metadata)", "userId (metadata)"]
_SHORT_EXTRA = ["duration (metadata)", "userId (metadata)", "email (metadata)"]
_DURATIONS = [("", 42), ("Greater than 6 months", 21), ("Less than 3 months", 27), ("3 to 6 months", 9),
              ("Long-term", 0.6), ("Short-term", 0.3), ("Long-duration", 0.02), ("Seasonal", 0.01), ("None", 0.01)]
_STATUS = [("canceled", 78), ("incomplete_expired", 11), ("active", 11), ("past_due", 0.12), ("unpaid", 0.03)]
_SUBSCRIPTION = [("monthly", 57), ("yearly", 27), ("", 13), ("usage", 3)]
_CURRENCY = [("usd", 94.4), ("inr", 3), ("eur", 2.1), ("gbp", 0.5)]
_YEAR = [(2021, 11), (2022, 27), (2023, 26), (2024, 20), (2025, 11), (2026, 5)]
_FEEDBACK = [("", 99.9), ("unused", 0.05), ("too_expensive", 0.02), ("too_complex", 0.01), ("other", 0.01)]
_PRODUCTS = [("Order Form - Basic", 52), ("Order Form - Premium", 24), ("Payment Form - Basic", 9),
             ("Order Form - Business", 3), ("WhatsApp Form - Basic", 3), ("Neartail - Startup Plan", 2.5),
             ("Payment Form - Premium", 3), ("Neartail - Pro Plan", 2), ("Formfacade - Basic", 1.5)]
_SECTOR = [("", 95), ("Retail Trade", 1.6), ("Accommodation and Food Services", 0.8), ("Educational Services", 0.7),
           ("Arts, Entertainment, and Recreation", 0.4), ("Other Services (except Public Administration)", 0.3),
           ("Agriculture, Forestry, Fishing and Hunting", 0.2), ("Manufacturing", 0.2)]
_COUNTRY = [("", 95.4), ("US", 2), ("CA", 0.25), ("ID", 0.2), ("IN", 0.2), ("GB", 0.2), ("AU", 0.15)]
_PRICE = {"": 36, "monthly": 36, "yearly": 336, "usage": 5}


def _subscription_rows(name, rows, columns, seed):
    rng = random.Random(seed)
    stamp = lambda day: day.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    out = []
    for i in range(rows):
        year = max(_pick(rng, _YEAR), 2023 if name == "SI" else 0)      # the real SI began later, so its report lists fewer years
        created = datetime(year, 1, 1) + timedelta(minutes=rng.randrange(60 * 24 * 360))
        status = _pick(rng, _STATUS)
        subscription = _pick(rng, _SUBSCRIPTION)
        currency = _pick(rng, _CURRENCY)
        customer = rng.randrange(7000)
        sector = _pick(rng, _SECTOR)
        cell = {
            "id": f"sub_{name}{i:06}", "Customer ID": f"cus_{customer:06}", "Customer Email": f"user{customer}@example.com",
            "Domain": f"site{customer % 1400}.example.com", "Domian": f"site{customer % 1400}.example.com",
            "Plan": f"price_{rng.randrange(55):02}", "Product": _pick(rng, _PRODUCTS), "Currency": currency,
            "Cancellation Reason": "cancellation_requested" if status == "canceled" and rng.random() < .3 else "",
            "Cancellation Feedback": _pick(rng, _FEEDBACK),
            "Amount": _PRICE[subscription] * rng.choice([1, 1, 1, 2, 4]) * (80 if currency == "inr" else 1),
            "Subscription": subscription, "Status": status, "Created (UTC)": stamp(created), "Year": year,
            "Start Date (UTC)": stamp(created), "Current Period Start (UTC)": stamp(created + timedelta(days=30)),
            "Current Period End (UTC)": stamp(created + timedelta(days=60)),
            "Canceled At (UTC)": stamp(created + timedelta(days=rng.randrange(1, 500))) if status == "canceled" else "",
            "Cancel At Period End": rng.choice(["TRUE", "FALSE"]), "Industry (metadata)": "",
            "duration (metadata)": _pick(rng, _DURATIONS),
            "socialmedia (metadata)": rng.choice(["", "", "", "", "instagram", "facebook", "whatsapp"]),
            "ecommerce (metadata)": rng.choice(["", "", "", "", "", "shopify", "woocommerce"]),
            "website (metadata)": rng.choice(["", "", "", "", "yes", "no"]),
            "state (metadata)": rng.choice(["", "", "", "", "", "", "CA", "TX", "NY", "FL"]),
            "country (metadata)": _pick(rng, _COUNTRY), "subcategory (metadata)": "", "subsector (metadata)": "",
            "sector (metadata)": sector, "industry (metadata)": "" if not sector else sector.split(" ")[0],
            "userid (metadata)": "", "categort (metadata)": "", "businss (metadata)": "",
            "category (metadata)": rng.choice(["", "", "", "", "", "Bakery", "Catering", "Boutique"]),
            "business (metadata)": rng.choice(["", "", "", "", "", "", "Bakery", "Events"]), "Business (metadata)": "",
            "email (metadata)": f"user{customer}@example.com", "userId (metadata)": f"u{customer}",
        }
        out.append([cell[c] for c in columns])
    return out


def _status_report(rows, columns):
    """A tab's pivot as the sheets importer reads it: the row of labels beneath the pivot's title is the header, and the pivot has
    only the statuses and years its tab holds. The second return is its Grand Total row, which the importer splits off."""
    years = sorted({row[columns.index("Year")] for row in rows})
    y, s = columns.index("Year"), columns.index("Status")
    statuses = [status for status in ("active", "canceled", "past_due", "unpaid") if any(r[s] == status for r in rows)]
    table = []
    for year in years:
        counts = [sum(1 for r in rows if r[y] == year and r[s] == status) for status in statuses]
        table.append([year, *counts, sum(counts)])
    totals = [sum(line[index] for line in table) for index in range(1, len(statuses) + 2)]
    return ["Year", *statuses, "Grand Total"], table, ["Grand Total", *totals]


def subscriptions():
    tables = []
    for index, (name, rows) in enumerate((("NT", 9741), ("SI", 631), ("FF", 5602))):
        columns = _BASE + (_NT_EXTRA if name == "NT" else _SHORT_EXTRA)
        if name == "FF":
            columns = ["Domian" if c == "Domain" else c for c in columns]
        data = _subscription_rows(name, rows, columns, 2026 + index)
        tables.append(_tab(name, columns, data))
        header, report, total = _status_report(data, columns)
        tables.append(_tab(f"{name} Report", header, report))
        if name != "FF":                                                # the real FF's pivot has no Grand Total row
            tables.append(_tab(f"{name} Report summaries", header, [total]))
    return tables


_KEYWORD_TOPICS = {
    "Checklist": ["fire extinguisher", "home inspection", "house inspection", "ppe safety", "townhouse inspection",
                  "4 point home", "aerial lift", "annual apartment", "balcony", "building", "city rental",
                  "commercial building", "condo", "construction safety", "crane", "dental osha"],
    "Forklift": ["forklift", "pallet jack", "reach truck", "telehandler", "scissor lift", "warehouse"],
    "Inspection": ["roof inspection", "elevator inspection", "boiler inspection", "vehicle inspection", "hvac inspection"],
    "Sheet1": ["safety audit", "compliance audit", "hazard report", "incident report", "toolbox talk"],
}
_KEYWORD_SUFFIX = ["checklist", "form", "template", "list", "audit checklist", "inspection form", "maintenance checklist",
                   "for buyers"]
_KEYWORD_MODIFIER = ["", "free", "printable", "online", "sample", "pdf", "excel"]
KEYWORD_COLUMNS = ["Keyword", "Currency", "Avg. monthly searches", "Three month change", "YoY change", "Competition",
                   "Competition (indexed value)", "Top of page bid (low range)", "Top of page bid (high range)",
                   "shortlist"]


def keywords():
    tables = []
    for index, (name, topics) in enumerate(_KEYWORD_TOPICS.items()):
        rng = random.Random(2026 + index)
        combos = sorted(f"{m} {t} {s}".strip() for t in topics for s in _KEYWORD_SUFFIX for m in _KEYWORD_MODIFIER)
        rows = []
        for keyword in rng.sample(combos, len(combos) * 3 // 4):
            low = round(rng.uniform(0.2, 5), 2)
            competition = rng.choice(["Low", "Low", "Low", "Medium", "High", ""])
            rows.append([keyword, "USD", rng.choice([0, 10, 10, 50, 50, 100, 100, 500, 500, 1000, 1000, 5000, 10000]),
                         f"{rng.choice([-90, -50, -30, -10, 0, 0, 10, 25, 50, 100])}%",
                         f"{rng.choice([-90, -50, -30, -10, 0, 0, 10, 25, 50, 100, 900])}%", competition,
                         rng.randrange(0, 101) if competition else "", low if competition else "",
                         round(low * rng.uniform(1.5, 4), 2) if competition else "",
                         rng.choice(["", "", "", "", "Yes"])])
        tables.append(_tab(name, KEYWORD_COLUMNS, rows))
    return tables


def orders():
    rng = random.Random(7101)
    products = [("Linen Throw", "LT-01", 48), ("Ceramic Mug", "CM-02", 16), ("Oak Tray", "OT-03", 34),
                ("Wool Socks", "WS-04", 12), ("Soy Candle", "SC-05", 22), ("Brass Hook", "BH-06", 9),
                ("Canvas Tote", "CT-07", 18)]
    provinces = {"US": ["CA", "NY", "TX", "WA", "IL"], "CA": ["ON", "BC", "QC"], "GB": ["ENG", "SCT"], "AU": ["NSW", "VIC"]}
    columns = ["Name", "Email", "Financial Status", "Paid at", "Fulfillment Status", "Accepts Marketing", "Currency",
               "Subtotal", "Shipping", "Taxes", "Total", "Discount Code", "Discount Amount", "Shipping Method",
               "Created at", "Lineitem quantity", "Lineitem name", "Lineitem price", "Lineitem sku", "Shipping City",
               "Shipping Province", "Shipping Country", "Vendor", "Source"]
    rows = []
    for i in range(2400):
        product, sku, price = rng.choice(products)
        quantity = rng.choice([1, 1, 1, 2, 2, 3, 5])
        subtotal = price * quantity
        code = _pick(rng, [("", 80), ("WELCOME10", 12), ("SPRING20", 8)])
        discount = round(subtotal * (0.1 if code == "WELCOME10" else 0.2 if code == "SPRING20" else 0), 2)
        method = _pick(rng, [("Standard", 70), ("Express", 18), ("Free Shipping", 12)])
        shipping = {"Standard": 6.5, "Express": 18, "Free Shipping": 0}[method]
        taxes = round((subtotal - discount) * 0.08, 2)
        country = _pick(rng, [("US", 70), ("CA", 14), ("GB", 10), ("AU", 6)])
        financial = _pick(rng, [("paid", 78), ("pending", 8), ("refunded", 7), ("partially_refunded", 3), ("voided", 4)])
        created = datetime(2023, 1, 1) + timedelta(minutes=rng.randrange(60 * 24 * 900))
        rows.append([f"#{1001 + i}", f"buyer{rng.randrange(1500)}@example.com", financial,
                     created.strftime("%Y-%m-%d %H:%M") if financial != "pending" else "",
                     _pick(rng, [("fulfilled", 72), ("unfulfilled", 22), ("partial", 6)]),
                     rng.choice(["yes", "no", "no"]), "USD", subtotal, shipping, taxes,
                     round(subtotal - discount + shipping + taxes, 2), code, discount, method,
                     created.strftime("%Y-%m-%d %H:%M"), quantity, product, price, sku, f"City{rng.randrange(40)}",
                     rng.choice(provinces[country]), country, "Hearth & Co",
                     _pick(rng, [("web", 62), ("pos", 14), ("instagram", 16), ("wholesale", 8)])])
    return [_tab("orders_export_1", columns, rows)]


def deals():
    rng = random.Random(7102)
    owners = _names(rng, 8)
    columns = ["Deal Name", "Stage", "Amount", "Close Date", "Deal Owner", "Lead Source", "Region", "Probability (%)", "Type",
               "Account"]
    chance = {"Prospecting": 10, "Qualification": 25, "Proposal": 50, "Negotiation": 80, "Closed Won": 100, "Closed Lost": 0}
    rows = []
    for i in range(1200):
        stage = _pick(rng, [("Prospecting", 14), ("Qualification", 13), ("Proposal", 11), ("Negotiation", 8),
                            ("Closed Won", 30), ("Closed Lost", 24)])
        rows.append([f"Deal {i + 1:04}", stage, rng.randrange(2, 400) * 250,
                     (date(2023, 1, 1) + timedelta(days=rng.randrange(1000))).isoformat(), rng.choice(owners),
                     _pick(rng, [("Inbound", 34), ("Referral", 18), ("Cold Call", 22), ("Partner", 14), ("Event", 12)]),
                     _pick(rng, [("NA", 44), ("EMEA", 30), ("APAC", 18), ("LATAM", 8)]), chance[stage],
                     _pick(rng, [("New Business", 55), ("Renewal", 30), ("Upsell", 15)]), f"Account {rng.randrange(300):03}"])
    teams = [["Owner", "Team", "Quota"]] + [[owner, rng.choice(["Hunters", "Farmers", "Strategic"]),
                                             rng.randrange(4, 16) * 50000] for owner in owners]
    return [_tab("Deals", columns, rows), _tab("Reps", teams[0], teams[1:])]


def roster():
    rng = random.Random(7103)
    columns = ["Emp #", "Full Name", "Dept", "Title", "Hire Dt", "Term Dt", "Status", "FT/PT", "Base Sal", "Bonus %", "Mgr",
               "Loc", "Gender", "Rating"]
    people = _names(rng, 500)
    managers = people[:9]
    rows = []
    for i, name in enumerate(people):
        department = _pick(rng, [("Eng", 34), ("Sales", 22), ("Mktg", 10), ("Fin", 8), ("HR", 6), ("Ops", 20)])
        status = _pick(rng, [("A", 76), ("T", 20), ("L", 4)])
        hired = date(2015, 1, 1) + timedelta(days=rng.randrange(3600))
        rows.append([1000 + i, name, department, rng.choice(["Associate", "Senior", "Lead", "Analyst", "Specialist"]),
                     hired.strftime("%m/%d/%Y"),
                     (hired + timedelta(days=rng.randrange(60, 1500))).strftime("%m/%d/%Y") if status == "T" else "",
                     status, _pick(rng, [("FT", 86), ("PT", 14)]), rng.randrange(40, 240) * 1000, rng.choice([0, 5, 10, 15, 20]),
                     rng.choice(managers), _pick(rng, [("NYC", 26), ("SFO", 22), ("LON", 18), ("BLR", 22), ("remote", 12)]),
                     _pick(rng, [("M", 52), ("F", 45), ("X", 3)]), rng.choice([1, 2, 3, 3, 3, 4, 4, 5])])
    return [_tab("Roster", columns, rows)]


def expenses():
    rng = random.Random(7104)
    vendors = {"Travel": ["Delta", "Marriott", "Uber"], "Meals": ["Sweetgreen", "DoorDash", "Blue Bottle"],
               "Software": ["Slack", "Figma", "GitHub", "Zoom"], "Office Supplies": ["Staples", "Amazon"],
               "Marketing": ["Google Ads", "Meta Ads"], "Utilities": ["Comcast", "PG&E"]}
    payers = _names(rng, 7)
    columns = ["Date", "Vendor", "Category", "Amt", "Paid By", "Reimbursed", "Project", "Receipt?", "Notes"]
    rows = []
    for _ in range(1800):
        category = _pick(rng, [("Travel", 24), ("Meals", 22), ("Software", 18), ("Office Supplies", 14), ("Marketing", 12),
                               ("Utilities", 10)])
        rows.append([(date(2023, 6, 1) + timedelta(days=rng.randrange(700))).isoformat(), rng.choice(vendors[category]), category,
                     f"${rng.randrange(500, 240000) / 100:,.2f}", rng.choice(payers), _pick(rng, [("Y", 60), ("N", 34), ("", 6)]),
                     rng.choice(["Apollo", "Borealis", "Cascade", ""]), rng.choice(["TRUE", "TRUE", "TRUE", "FALSE"]),
                     rng.choice(["", "", "", "client visit", "team offsite", "renewal"])])
    return [_tab("Expenses 2024", columns, rows)]


def tickets():
    rng = random.Random(7105)
    agents = _names(rng, 9)
    columns = ["Ticket ID", "Subject", "Priority", "Status", "Channel", "Assignee", "Created", "First Response (min)",
               "Resolution (hrs)", "CSAT", "Product Area", "Customer Tier"]
    rows = []
    for i in range(3000):
        status = _pick(rng, [("Open", 12), ("Pending", 10), ("Solved", 48), ("Closed", 30)])
        done = status in ("Solved", "Closed")
        rows.append([f"T-{50000 + i}", f"Issue {rng.randrange(200)}", _pick(rng, [("P1", 6), ("P2", 18), ("P3", 50), ("P4", 26)]),
                     status, _pick(rng, [("Email", 44), ("Chat", 30), ("Phone", 14), ("Web", 12)]), rng.choice(agents),
                     (datetime(2024, 1, 1) + timedelta(minutes=rng.randrange(60 * 24 * 600))).strftime("%Y-%m-%d %H:%M"),
                     rng.randrange(1, 480), round(rng.uniform(0.2, 120), 1) if done else "",
                     rng.choice([1, 2, 3, 4, 4, 5, 5, 5, "", ""]) if done else "",
                     _pick(rng, [("Billing", 22), ("Login", 18), ("Reports", 20), ("Integrations", 24), ("Mobile", 16)]),
                     _pick(rng, [("Free", 40), ("Pro", 40), ("Enterprise", 20)])])
    return [_tab("Tickets", columns, rows)]


def inventory():
    rng = random.Random(7106)
    suppliers = [("Norder GmbH", "DE", 21), ("Kessel AG", "DE", 28), ("Pacific Fasteners", "US", 7), ("Lone Star Tool", "US", 9),
                 ("Shenzhen Volt", "CN", 45), ("Maple Paint Co", "CA", 12)]
    categories = {"Hand Tools": ["Hammer", "Wrench", "Pliers", "Chisel"], "Fasteners": ["Hex Bolt", "Wood Screw", "Lock Washer"],
                  "Electrical": ["Wire Spool", "Junction Box", "Breaker"], "Paint": ["Primer", "Enamel", "Thinner"]}
    columns = ["SKU", "Description", "Cat", "QOH", "ROP", "Unit Cost", "Unit Price", "Supplier", "WH", "Discontinued",
               "Last Count"]
    rows = []
    for i in range(900):
        category = rng.choice(list(categories))
        cost = round(rng.uniform(0.5, 90), 2)
        rows.append([f"SKU{10000 + i}", f"{rng.choice(categories[category])} {rng.randrange(1, 60)}", category,
                     rng.choice([0, 0, 3, 8, 12, 25, 40, 75, 120, 300]), rng.choice([5, 10, 20, 50]), cost,
                     round(cost * rng.uniform(1.2, 2.6), 2), rng.choice(suppliers)[0],
                     rng.choice(["East", "West", "Central"]), rng.choice([0, 0, 0, 0, 0, 1]),
                     (date(2025, 1, 1) + timedelta(days=rng.randrange(300))).isoformat()])
    return [_tab("Items", columns, rows),
            _tab("Suppliers", ["Supplier", "Country", "Lead Time (days)"], [list(s) for s in suppliers])]


def marks():
    rng = random.Random(7107)
    students = _names(rng, 150)
    teachers = dict(zip(["Mathematics", "Science", "English", "History", "Biology", "Computer Science"], _names(rng, 6)))
    columns = ["Roll No", "Student", "Class", "Subject", "Marks", "Max Marks", "Attendance %", "Result", "Teacher", "Term"]
    rows = []
    for number, student in enumerate(students):
        grade = rng.choice(["VIII", "IX", "X"])
        for subject, teacher in teachers.items():
            for term in ("Term 1", "Term 2"):
                absent = rng.random() < 0.04
                score = 0 if absent else rng.randrange(18, 101)
                rows.append([2000 + number, student, grade, subject, "" if absent else score, 100, rng.randrange(55, 100),
                             "Absent" if absent else ("Pass" if score >= 35 else "Fail"), teacher, term])
    return [_tab("Marksheet", columns, rows)]


def appointments():
    rng = random.Random(7108)
    doctors = {"Cardiology": ["Dr. Rao", "Dr. Hale"], "Pediatrics": ["Dr. Lim", "Dr. Osei"], "Dermatology": ["Dr. Stone"],
               "Orthopedics": ["Dr. Marsh", "Dr. Ivers"], "Ophthalmology": ["Dr. Quill"], "General Medicine": ["Dr. Bose", "Dr. Ford"]}
    columns = ["Appt ID", "Patient", "Doctor", "Specialty", "Visit Date", "Slot", "No Show", "Fee", "Insurance", "New Patient",
               "Follow-up Reqd"]
    rows = []
    for i in range(2200):
        specialty = _pick(rng, [("Cardiology", 14), ("Pediatrics", 22), ("Dermatology", 12), ("Orthopedics", 16),
                                ("Ophthalmology", 10), ("General Medicine", 26)])
        rows.append([f"A{70000 + i}", f"Patient {rng.randrange(1400):04}", rng.choice(doctors[specialty]), specialty,
                     (date(2023, 9, 1) + timedelta(days=rng.randrange(800))).strftime("%d-%b-%Y"),
                     rng.choice(["Morning", "Afternoon", "Evening"]), _pick(rng, [("N", 88), ("Y", 12)]),
                     rng.choice([40, 60, 80, 120, 150, 200]), _pick(rng, [("Aetna", 26), ("Cigna", 22), ("Humana", 16), ("None", 36)]),
                     rng.choice(["TRUE", "FALSE", "FALSE"]), rng.choice(["Yes", "No", "No", "No"])])
    return [_tab("Appointments", columns, rows)]


BUILDERS = {"subscriptions": subscriptions, "keywords": keywords, "orders": orders, "deals": deals, "roster": roster,
            "expenses": expenses, "tickets": tickets, "inventory": inventory, "marks": marks,
            "appointments": appointments}


@lru_cache(maxsize=None)
def workbook(name):
    """The tables of one workbook, as an upload hands them to the engine: every cell a string."""
    return BUILDERS[name]()
