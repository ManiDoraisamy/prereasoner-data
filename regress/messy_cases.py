"""Questions on regress/messy_workbooks.py as people ask them: neither a column's name nor a cell's value as written. The
expected answer is computed here from the workbook's rows, never from the engine, and an answer is graded on its value.

An expected answer is one of:
  ("scalar", n or [n, ...])     one row holding this number (any of them, when the wording allows more than one), within 0.5%
  ("groups", {label: n})        the rows hold each label with its number
  ("first", [label, ...])       the first row names one of these labels (more than one on a tie)
  ("rows", n)                   the answer lists n rows
  ("money", {currency: n})      a total of mixed money: one row per currency

A case's ``family`` says what the wording leans on: "value" a stored value named by its meaning, "column" a column named by
its meaning, "structure" a shape of question, "plain" a control that uses the data's own words. The split is by the
question's hash on the workbooks written for this benchmark (half "frozen", scored in totals only); the owner's two sheet
shapes were studied while the fixes of 2026-10-30 were made, so they are always "dev".
"""
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass

from regress.messy_workbooks import workbook

STUDIED = ("subscriptions", "keywords")
TOLERANCE = 0.005


@dataclass(frozen=True)
class Case:
    workbook: str
    question: str
    expected: tuple
    family: str

    @property
    def split(self):
        if self.workbook in STUDIED:
            return "dev"
        return "frozen" if hashlib.sha256(self.question.encode("utf-8")).digest()[0] % 2 else "dev"


def number(value):
    """The number a cell holds as a person reads it ("$1,234.50", "25%"), else None."""
    try:
        return float(str(value).replace(",", "").replace("%", "").replace("$", "").strip())
    except ValueError:
        return None


class Sheet:
    """One table's rows by column name, and the sums a question's answer is computed with."""

    def __init__(self, table):
        self.rows = [dict(zip(table["columns"], row)) for row in table["rows"]]

    def select(self, test=None):
        return [r for r in self.rows if test is None or test(r)]

    def count(self, test=None):
        return len(self.select(test))

    def total(self, column, test=None):
        return sum(number(r[column]) or 0.0 for r in self.select(test))

    def mean(self, column, test=None):
        values = [number(r[column]) for r in self.select(test) if number(r[column]) is not None]
        return sum(values) / len(values)

    def extreme(self, column, test=None, fn=max):
        return fn(number(r[column]) for r in self.select(test) if number(r[column]) is not None)

    def labels_at(self, label, column, test=None, fn=max):
        """The labels of the rows at the extreme of ``column``."""
        best = self.extreme(column, test, fn)
        return sorted({r[label] for r in self.select(test) if number(r[column]) == best})

    def by(self, key, column=None, test=None, how="sum"):
        """{label: total of ``column`` (rows without one), or its mean} over the rows that have a label."""
        totals, counts = defaultdict(float), defaultdict(int)
        for r in self.select(test):
            if r[key] != "":
                value = 1.0 if column is None else number(r[column])
                if value is not None:
                    totals[r[key]] += value
                    counts[r[key]] += 1
        return {k: (v / counts[k] if how == "mean" else v) for k, v in totals.items()}

    def top(self, key, column=None, test=None):
        """The labels with the most rows, or the largest total of ``column``."""
        table = self.by(key, column, test)
        return sorted(k for k, v in table.items() if v == max(table.values()))


def equals(column, *values):
    return lambda r: r[column] in values


def year_of(column, year):
    return lambda r: str(year) in r[column]


def _subscriptions(tables):
    nt = Sheet(tables[0])
    duration, status, kind = "duration (metadata)", "Status", "Subscription"

    def money(test=None):
        return ("money", nt.by("Currency", "Amount", test))

    def count(test):
        customers = {r["Customer ID"] for r in nt.select(test)}
        return ("scalar", [nt.count(test), len(customers)])

    usd = equals("Currency", "usd")
    both = lambda r: r["Subscription"] in ("monthly", "yearly")  # noqa: E731
    return [
        ("how much sales for long term customers (above 6 months)", money(equals(duration, "Greater than 6 months")), "value"),
        ("revenue from people who stayed longer than half a year", money(equals(duration, "Greater than 6 months")), "value"),
        ("sales from short term customers, under 3 months", money(equals(duration, "Less than 3 months")), "value"),
        ("revenue from mid-term customers between 3 and 6 months", money(equals(duration, "3 to 6 months")), "value"),
        ("how many people cancelled", count(equals(status, "canceled")), "value"),
        ("how many customers have churned", count(equals(status, "canceled")), "value"),
        ("how many subscriptions are still live", count(equals(status, "active")), "value"),
        ("how many subs are overdue on payment", count(equals(status, "past_due")), "value"),
        ("how many customers pay annually", count(equals(kind, "yearly")), "value"),
        ("monthly vs yearly subscribers", ("groups", nt.by(kind, test=both)), "structure"),
        ("which product brings in the most money", ("first", nt.top("Product", "Amount", usd) + nt.top("Product", "Amount")), "column"),
        ("total sales split by sector", ("groups", nt.by("sector (metadata)", "Amount")), "column"),
        ("sales from retailers", money(equals("sector (metadata)", "Retail Trade")), "value"),
        ("how much do schools pay us", money(equals("sector (metadata)", "Educational Services")), "value"),
        ("churn by year", ("groups", nt.by("Year", test=equals(status, "canceled"))), "structure"),
        ("how many signups in 2024", count(equals("Year", "2024")), "column"),
        ("revenue last year (2025)", money(equals("Year", "2025")), "column"),
        ("who is our biggest customer by spend", ("first", nt.top("Customer ID", "Amount") + nt.top("Customer ID", "Amount", usd)), "column"),
        ("how many customers are from the US", count(equals("country (metadata)", "US")), "plain"),
        ("how many will drop off at the end of their billing cycle", count(equals("Cancel At Period End", "TRUE")), "column"),
        ("how many said it was too pricey", count(equals("Cancellation Feedback", "too_expensive")), "value"),
        ("how much did we make in rupees", money(equals("Currency", "inr")), "value"),
    ]


def _keywords(tables):
    s = Sheet(tables[0])
    searches, three, yoy, competition, low, high, short, word = ("Avg. monthly searches", "Three month change", "YoY change",
                                                                 "Competition", "Top of page bid (low range)",
                                                                 "Top of page bid (high range)", "shortlist", "Keyword")
    positive = lambda column: (lambda r: (number(r[column]) or 0) > 0)  # noqa: E731
    return [
        ("which keywords get the most traffic", ("first", s.labels_at(word, searches)), "column"),
        ("what are the priciest terms to advertise on", ("first", s.labels_at(word, high)), "column"),
        ("how many easy to rank for keywords do we have", ("scalar", s.count(equals(competition, "Low"))), "value"),
        ("how many low comp terms are there", ("scalar", s.count(equals(competition, "Low"))), "value"),
        ("average cpc for high competition terms", ("scalar", s.mean(high, equals(competition, "High"))), "column"),
        ("how many terms are growing year over year", ("scalar", s.count(positive(yoy))), "structure"),
        ("how many keywords have zero volume", ("scalar", s.count(lambda r: number(r[searches]) == 0)), "column"),
        ("total volume for fire extinguisher stuff", ("scalar", s.total(searches, lambda r: "fire extinguisher" in r[word])), "column"),
        ("what did we shortlist", ("rows", s.count(equals(short, "Yes"))), "value"),
        ("which term fell the most yoy", ("first", s.labels_at(word, yoy, fn=min)), "column"),
        ("cheapest shortlisted term to bid on", ("first", s.labels_at(word, low, equals(short, "Yes"), fn=min)), "column"),
        ("search volume by how competitive it is", ("groups", s.by(competition, searches)), "column"),
        ("keywords with over 1000 searches a month and not much competition",
         ("rows", s.count(lambda r: (number(r[searches]) or 0) > 1000 and r[competition] == "Low")), "value"),
        ("how many terms mention crane", ("scalar", s.count(lambda r: "crane" in r[word])), "structure"),
        ("avg searches across everything", ("scalar", s.mean(searches)), "column"),
        ("what's trending up in the last 3 months", ("rows", s.count(positive(three))), "structure"),
    ]


def _orders(tables):
    s = Sheet(tables[0])
    money, paid, shipped, country = "Total", "Financial Status", "Fulfillment Status", "Shipping Country"
    return [
        ("how much money did we give back to customers",
         ("scalar", [s.total(money, equals(paid, "refunded")), s.total(money, equals(paid, "refunded", "partially_refunded"))]), "value"),
        ("orders that havent gone out yet", ("scalar", s.count(equals(shipped, "unfulfilled"))), "value"),
        ("how many people opted in to our emails", ("scalar", s.count(equals("Accepts Marketing", "yes"))), "column"),
        ("what's our aov", ("scalar", s.mean(money)), "column"),
        ("average spend per checkout", ("scalar", s.mean(money)), "column"),
        ("best selling product", ("first", s.top("Lineitem name", "Lineitem quantity") + s.top("Lineitem name")), "column"),
        ("sales from canada", ("scalar", s.total(money, equals(country, "CA"))), "value"),
        ("how many used a promo", ("scalar", s.count(lambda r: r["Discount Code"] != "")), "structure"),
        ("revenue by sales channel", ("groups", s.by("Source", money)), "column"),
        ("how many orders are still waiting on payment", ("scalar", s.count(equals(paid, "pending"))), "value"),
        ("biggest order we ever got", ("first", s.labels_at("Name", money)), "column"),
        ("how much did customers pay for delivery", ("scalar", s.total("Shipping")), "column"),
        ("sales tax collected", ("scalar", s.total("Taxes")), "column"),
        ("how many chose fast delivery", ("scalar", s.count(equals("Shipping Method", "Express"))), "value"),
        ("which state orders the most", ("first", s.top("Shipping Province")), "column"),
        ("how much in coupons did we hand out", ("scalar", s.total("Discount Amount")), "column"),
        ("orders from instagram", ("scalar", s.count(equals("Source", "instagram"))), "plain"),
        ("orders placed in 2024", ("scalar", s.count(year_of("Created at", 2024))), "plain"),
        ("total sales", ("scalar", s.total(money)), "plain"),
        ("how many units did we sell", ("scalar", s.total("Lineitem quantity")), "column"),
        ("how many payments were cancelled", ("scalar", s.count(equals(paid, "voided"))), "value"),
    ]


def _deals(tables):
    s, reps = Sheet(tables[0]), Sheet(tables[1])
    stage, money, region = "Stage", "Amount", "Region"
    won = equals(stage, "Closed Won")
    open_deal = lambda r: not r[stage].startswith("Closed")  # noqa: E731
    return [
        ("how many deals did we win", ("scalar", s.count(won)), "value"),
        ("how much did we lose", ("scalar", s.total(money, equals(stage, "Closed Lost"))), "value"),
        ("how much is still in play", ("scalar", s.total(money, open_deal)), "structure"),
        ("who's the top rep", ("first", s.top("Deal Owner", money, won) + s.top("Deal Owner", money)), "column"),
        ("revenue from europe", ("scalar", [s.total(money, equals(region, "EMEA")),
                                            s.total(money, lambda r: won(r) and r[region] == "EMEA")]), "value"),
        ("how many deals came from word of mouth", ("scalar", s.count(equals("Lead Source", "Referral"))), "value"),
        ("how many came from cold outreach", ("scalar", s.count(equals("Lead Source", "Cold Call"))), "value"),
        ("biggest deal", ("first", s.labels_at("Deal Name", money)), "column"),
        ("deals closing in 2025", ("scalar", s.count(year_of("Close Date", 2025))), "plain"),
        ("how much new logo revenue", ("scalar", [s.total(money, equals("Type", "New Business")),
                                                  s.total(money, lambda r: won(r) and r["Type"] == "New Business")]), "value"),
        ("how many expansion deals", ("scalar", s.count(equals("Type", "Upsell"))), "value"),
        ("pipeline by rep", ("groups", s.by("Deal Owner", money)), "column"),
        ("average deal size", ("scalar", s.mean(money)), "column"),
        ("how many deals are likely to close, over 75 percent", ("scalar", s.count(lambda r: number(r["Probability (%)"]) > 75)), "column"),
        ("how many deals are in contract talks", ("scalar", s.count(equals(stage, "Negotiation"))), "value"),
        ("deals by geography", ("groups", s.by(region)), "column"),
        ("how much came through partners", ("scalar", [s.total(money, equals("Lead Source", "Partner")),
                                                       s.total(money, lambda r: won(r) and r["Lead Source"] == "Partner")]), "plain"),
        ("how many early stage deals are there", ("scalar", [s.count(equals(stage, "Prospecting")),
                                                             s.count(equals(stage, "Prospecting", "Qualification"))]), "value"),
        ("asia revenue", ("scalar", [s.total(money, equals(region, "APAC")),
                                     s.total(money, lambda r: won(r) and r[region] == "APAC")]), "value"),
        ("how many renewals", ("scalar", s.count(equals("Type", "Renewal"))), "plain"),
        ("what is everyone's target added up", ("scalar", reps.total("Quota")), "column"),
    ]


def _roster(tables):
    s = Sheet(tables[0])
    pay, department, office = "Base Sal", "Dept", "Loc"
    manager = s.rows[0]["Mgr"]
    return [
        ("how many people left the company", ("scalar", s.count(equals("Status", "T"))), "value"),
        ("avg pay in engineering", ("scalar", s.mean(pay, equals(department, "Eng"))), "value"),
        ("headcount by office", ("groups", s.by(office)), "column"),
        ("how many part timers do we have", ("scalar", s.count(equals("FT/PT", "PT"))), "value"),
        ("who is on leave right now", ("rows", s.count(equals("Status", "L"))), "value"),
        ("highest paid person", ("first", s.labels_at("Full Name", pay)), "column"),
        ("total payroll", ("scalar", s.total(pay)), "column"),
        ("how many women work here", ("scalar", s.count(equals("Gender", "F"))), "value"),
        ("how many people in marketing", ("scalar", s.count(equals(department, "Mktg"))), "value"),
        ("how many in the london office", ("scalar", s.count(equals(office, "LON"))), "value"),
        ("how many work from home", ("scalar", s.count(equals(office, "remote"))), "value"),
        ("how many people have a perfect review", ("scalar", s.count(equals("Rating", "5"))), "column"),
        ("how many joined in 2019", ("scalar", s.count(year_of("Hire Dt", 2019))), "column"),
        ("average incentive", ("scalar", s.mean("Bonus %")), "column"),
        ("which team pays the most on average", ("first", [max(s.by(department, pay, how="mean").items(), key=lambda kv: kv[1])[0]]), "structure"),
        ("what do we pay the finance folks in total", ("scalar", s.total(pay, equals(department, "Fin"))), "value"),
        (f"how many report to {manager}", ("scalar", s.count(equals("Mgr", manager))), "column"),
        ("how many earn over 150k", ("scalar", s.count(lambda r: number(r[pay]) > 150000)), "structure"),
        ("how many full timers in bangalore", ("scalar", s.count(lambda r: r["FT/PT"] == "FT" and r[office] == "BLR")), "value"),
        ("how many people are still with us", ("scalar", s.count(equals("Status", "A"))), "value"),
    ]


def _expenses(tables):
    s = Sheet(tables[0])
    amount, category, paid_back = "Amt", "Category", "Reimbursed"
    return [
        ("how much did we spend on flights and hotels", ("scalar", s.total(amount, equals(category, "Travel"))), "value"),
        ("food and drinks spend", ("scalar", s.total(amount, equals(category, "Meals"))), "value"),
        ("saas costs", ("scalar", s.total(amount, equals(category, "Software"))), "value"),
        ("how much is still owed back to staff", ("scalar", s.total(amount, equals(paid_back, "N"))), "value"),
        ("who do we pay the most", ("first", s.top("Vendor", amount)), "column"),
        ("how many expenses have no proof of purchase", ("scalar", s.count(equals("Receipt?", "FALSE"))), "value"),
        ("spend by person", ("groups", s.by("Paid By", amount)), "column"),
        ("total spend", ("scalar", s.total(amount)), "column"),
        ("avg expense", ("scalar", s.mean(amount)), "column"),
        ("largest single expense", ("scalar", s.extreme(amount)), "column"),
        ("how much on ads", ("scalar", s.total(amount, equals(category, "Marketing"))), "value"),
        ("electricity and internet bills", ("scalar", s.total(amount, equals(category, "Utilities"))), "value"),
        ("pens paper and stuff", ("scalar", s.total(amount, equals(category, "Office Supplies"))), "value"),
        ("spending on apollo", ("scalar", s.total(amount, equals("Project", "Apollo"))), "plain"),
        ("breakdown by type of expense", ("groups", s.by(category, amount)), "column"),
        ("how many amazon purchases", ("scalar", s.count(equals("Vendor", "Amazon"))), "plain"),
        ("what did we pay uber", ("scalar", s.total(amount, equals("Vendor", "Uber"))), "plain"),
        ("how many have been paid back already", ("scalar", s.count(equals(paid_back, "Y"))), "value"),
        ("spend in 2024", ("scalar", s.total(amount, year_of("Date", 2024))), "plain"),
    ]


def _tickets(tables):
    s = Sheet(tables[0])
    priority, status, channel, tier = "Priority", "Status", "Channel", "Customer Tier"
    return [
        ("how many urgent tickets", ("scalar", s.count(equals(priority, "P1"))), "value"),
        ("how many are still unresolved", ("scalar", [s.count(equals(status, "Open")), s.count(equals(status, "Open", "Pending"))]), "value"),
        ("average time to fix", ("scalar", s.mean("Resolution (hrs)")), "column"),
        ("who handled the most", ("first", s.top("Assignee")), "column"),
        ("how many unhappy customers", ("scalar", [s.count(lambda r: number(r["CSAT"]) in (1.0, 2.0)), s.count(equals("CSAT", "1"))]), "structure"),
        ("how many phone calls", ("scalar", s.count(equals(channel, "Phone"))), "value"),
        ("tickets from big accounts", ("scalar", s.count(equals(tier, "Enterprise"))), "value"),
        ("how fast do we reply on average", ("scalar", s.mean("First Response (min)")), "column"),
        ("tickets per agent", ("groups", s.by("Assignee")), "column"),
        ("how many from free users", ("scalar", s.count(equals(tier, "Free"))), "plain"),
        ("billing problems count", ("scalar", s.count(equals("Product Area", "Billing"))), "plain"),
        ("tickets by urgency", ("groups", s.by(priority)), "column"),
        ("how many are done", ("scalar", [s.count(equals(status, "Solved")), s.count(equals(status, "Solved", "Closed"))]), "value"),
        ("average satisfaction", ("scalar", s.mean("CSAT")), "column"),
        ("slowest ticket to close", ("first", s.labels_at("Ticket ID", "Resolution (hrs)")), "column"),
        ("how many came through live chat", ("scalar", s.count(equals(channel, "Chat"))), "plain"),
        ("how many were logged in 2025", ("scalar", s.count(year_of("Created", 2025))), "plain"),
        ("how many low priority ones", ("scalar", s.count(equals(priority, "P4"))), "value"),
        ("what part of the product gets the most complaints", ("first", s.top("Product Area")), "column"),
        ("how many tickets are waiting on the customer", ("scalar", s.count(equals(status, "Pending"))), "value"),
    ]


def _inventory(tables):
    s, suppliers = Sheet(tables[0]), Sheet(tables[1])
    on_hand, cost, price, warehouse, category = "QOH", "Unit Cost", "Unit Price", "WH", "Cat"
    return [
        ("how many items are out of stock", ("scalar", s.count(equals(on_hand, "0"))), "column"),
        ("most expensive thing for us to buy", ("first", s.labels_at("Description", cost) + s.labels_at("SKU", cost)), "column"),
        ("what do we sell for the highest price", ("first", s.labels_at("Description", price) + s.labels_at("SKU", price)), "column"),
        ("how many items do we no longer carry", ("scalar", s.count(equals("Discontinued", "1"))), "value"),
        ("total units on hand", ("scalar", s.total(on_hand)), "column"),
        ("stock in the west warehouse", ("scalar", s.total(on_hand, equals(warehouse, "West"))), "column"),
        ("items per vendor", ("groups", s.by("Supplier")), "column"),
        ("average selling price", ("scalar", s.mean(price)), "column"),
        ("how many hand tools", ("scalar", s.count(equals(category, "Hand Tools"))), "plain"),
        ("how many screws and bolts", ("scalar", s.count(equals(category, "Fasteners"))), "value"),
        ("how many electrical parts", ("scalar", s.count(equals(category, "Electrical"))), "plain"),
        ("average purchase cost by warehouse", ("groups", s.by(warehouse, cost, how="mean")), "column"),
        ("who supplies the most products", ("first", s.top("Supplier")), "column"),
        ("how many vendors are german", ("scalar", suppliers.count(equals("Country", "DE"))), "value"),
        ("which vendor takes longest to deliver", ("first", suppliers.labels_at("Supplier", "Lead Time (days)")), "column"),
        ("which items cost us less than 1 dollar", ("rows", s.count(lambda r: number(r[cost]) < 1)), "column"),
        ("how many paint products", ("scalar", s.count(equals(category, "Paint"))), "plain"),
        ("how many items need reordering", ("scalar", s.count(lambda r: number(r[on_hand]) <= number(r["ROP"]))), "structure"),
        ("how many items are in central", ("scalar", s.count(equals(warehouse, "Central"))), "plain"),
        ("how many spray-can type coatings do we stock", ("scalar", s.count(equals(category, "Paint"))), "value"),
    ]


def _marks(tables):
    s = Sheet(tables[0])
    score, result, subject, attendance = "Marks", "Result", "Subject", "Attendance %"
    return [
        ("how many flunked", ("scalar", [s.count(equals(result, "Fail")), len({r["Student"] for r in s.select(equals(result, "Fail"))})]), "value"),
        ("who topped", ("first", s.labels_at("Student", score)), "column"),
        ("average score in maths", ("scalar", s.mean(score, equals(subject, "Mathematics"))), "value"),
        ("how many skipped the exam", ("scalar", s.count(equals(result, "Absent"))), "value"),
        ("how many had poor attendance, below 60", ("scalar", s.count(lambda r: number(r[attendance]) < 60)), "column"),
        ("average score by grade", ("groups", s.by("Class", score, how="mean")), "column"),
        ("how many cleared science", ("scalar", s.count(lambda r: r[result] == "Pass" and r[subject] == "Science")), "value"),
        ("how many history results", ("scalar", s.count(equals(subject, "History"))), "plain"),
        ("how many students in tenth grade", ("scalar", [s.count(equals("Class", "X")), len({r["Student"] for r in s.select(equals("Class", "X"))})]), "value"),
        ("which instructor has the most results", ("first", s.top("Teacher")), "column"),
        ("best score in english", ("scalar", s.extreme(score, equals(subject, "English"))), "column"),
        ("first term average", ("scalar", s.mean(score, equals("Term", "Term 1"))), "value"),
        ("how many scored above 90", ("scalar", s.count(lambda r: (number(r[score]) or 0) > 90)), "plain"),
        ("average attendance", ("scalar", s.mean(attendance)), "plain"),
        ("failures per subject", ("groups", s.by(subject, test=equals(result, "Fail"))), "structure"),
        ("computer class average", ("scalar", s.mean(score, equals(subject, "Computer Science"))), "value"),
        ("bio average", ("scalar", s.mean(score, equals(subject, "Biology"))), "value"),
        ("lowest attendance anyone had", ("scalar", s.extreme(attendance, fn=min)), "column"),
        ("how many results for class 9", ("scalar", s.count(equals("Class", "IX"))), "value"),
        ("how many exams were cleared", ("scalar", s.count(equals(result, "Pass"))), "value"),
    ]


def _appointments(tables):
    s = Sheet(tables[0])
    specialty, fee, insurance, new = "Specialty", "Fee", "Insurance", "New Patient"
    insured = lambda r: r[insurance] != "None"  # noqa: E731
    return [
        ("how many didn't turn up", ("scalar", s.count(equals("No Show", "Y"))), "value"),
        ("how many saw a heart doctor", ("scalar", s.count(equals(specialty, "Cardiology"))), "value"),
        ("how many kids visits", ("scalar", s.count(equals(specialty, "Pediatrics"))), "value"),
        ("total fees for skin problems", ("scalar", s.total(fee, equals(specialty, "Dermatology"))), "value"),
        ("how many had no cover", ("scalar", s.count(equals(insurance, "None"))), "value"),
        ("first time visitors", ("scalar", s.count(equals(new, "TRUE"))), "value"),
        ("revenue by physician", ("groups", s.by("Doctor", fee)), "column"),
        ("busiest physician", ("first", s.top("Doctor")), "column"),
        ("average consultation charge", ("scalar", s.mean(fee)), "column"),
        ("how many morning appointments", ("scalar", s.count(equals("Slot", "Morning"))), "plain"),
        ("how many need to come back", ("scalar", s.count(equals("Follow-up Reqd", "Yes"))), "column"),
        ("visits in 2024", ("scalar", s.count(year_of("Visit Date", 2024))), "plain"),
        ("which payer covers the most visits", ("first", s.top(insurance, test=insured) + s.top(insurance)), "column"),
        ("how many bone specialist visits", ("scalar", s.count(equals(specialty, "Orthopedics"))), "value"),
        ("how many eye checkups", ("scalar", s.count(equals(specialty, "Ophthalmology"))), "value"),
        ("how many returning patients", ("scalar", s.count(equals(new, "FALSE"))), "value"),
        ("most anyone paid for one visit", ("scalar", s.extreme(fee)), "column"),
        ("fees from aetna", ("scalar", s.total(fee, equals(insurance, "Aetna"))), "plain"),
        ("visits per department", ("groups", s.by(specialty)), "column"),
        ("how many appointments did Dr. Rao have", ("scalar", s.count(equals("Doctor", "Dr. Rao"))), "plain"),
    ]


_QUESTIONS = {"subscriptions": _subscriptions, "keywords": _keywords, "orders": _orders, "deals": _deals, "roster": _roster,
              "expenses": _expenses, "tickets": _tickets, "inventory": _inventory, "marks": _marks,
              "appointments": _appointments}


def cases(names=None):
    """Every case of the named workbooks (all by default), in order."""
    return [Case(name, question, expected, family) for name in (names or _QUESTIONS)
            for question, expected, family in _QUESTIONS[name](workbook(name))]


def _close(value, gold):
    return abs(value - gold) <= max(TOLERANCE * abs(gold), TOLERANCE)


def _labelled(rows):
    """{label: its row's numbers} of a result's rows."""
    found = {}
    for row in rows:
        labels = [str(v) for v in row if number(v) is None and str(v) != "None"]
        values = [number(v) for v in row if number(v) is not None]
        if labels and values:
            found[labels[0]] = values
    return found


def grade(expected, response):
    """"right", "asked" or "wrong", and a note. An answer that asks back is not wrong, and not right."""
    kind, gold = expected
    result = response.get("result") or {}
    rows = result.get("rows") or []
    if response.get("clarify") or response.get("error") or not result:
        return "asked", str(response.get("reason") or response.get("error") or "no answer")[:300]
    cells = [[number(v) for v in row if number(v) is not None] for row in rows]
    if kind == "scalar":
        golds = gold if isinstance(gold, (list, tuple)) else [gold]
        # "first time visitors": the rows themselves answer a count as well as their number does.
        right = (len(rows) == 1 and any(_close(v, g) for v in cells[0] for g in golds)) or (len(rows) > 1 and len(rows) in golds)
    elif kind == "money":
        labelled = _labelled(rows)
        if len(gold) == 1:
            right = len(rows) == 1 and any(_close(v, g) for v in cells[0] for g in gold.values())
        else:
            right = all(k in labelled and any(_close(x, v) for x in labelled[k]) for k, v in gold.items())
    elif kind == "first":
        right = bool(rows) and any(str(v) in {str(g) for g in gold} for v in rows[0])
    elif kind == "rows":
        right = len(rows) == gold
    elif kind == "groups":
        labelled = _labelled(rows)
        right = bool(gold) and all(k in labelled and any(_close(x, v) for x in labelled[k]) for k, v in gold.items())
    else:
        raise ValueError(kind)
    return ("right" if right else "wrong"), f"{len(rows)} rows, first {re.sub(r'\s+', ' ', str(rows[:1]))[:200]}"
