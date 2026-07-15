"""World-model eval cases. Each case = an uploaded CSV table (or two) + a natural-language question +
an ORACLE SQL. The oracle computes the EXPECTED answer by JOINING the clean world-model tables
(world."Countries" / "Cities" / "Elements") against the upload — so the gold is derived from the world DB
at eval time, not hand-labelled. PreReasoner runs its own resolve+route+view-stack path; run.py compares.

The oracle references each uploaded table by its `name` (run.py exposes them as CTEs) and the world tables
directly. Test values use canonical entity names that exist in the clean world tables so the exact-name
oracle join is itself unambiguous ground truth; city cases disambiguate collisions by max population (the
prominent city), matching how the resolver picks Paris->France over the US Parises.
"""
SALES = {"name": "sales", "columns": ["country", "amount"],
         "rows": [["France", 120], ["Germany", 80], ["China", 200], ["India", 50],
                  ["United States", 300], ["Brazil", 90], ["Japan", 60]]}
CUSTOMERS = {"name": "customers", "columns": ["name", "city"],
             "rows": [["Ada", "Paris"], ["Lin", "Lyon"], ["Bo", "Berlin"], ["Mei", "Shanghai"]]}
ORDERS = {"name": "orders", "columns": ["customer", "amount"],
          "rows": [["Ada", 120], ["Lin", 150], ["Bo", 200], ["Mei", 90]]}
SAMPLES = {"name": "samples", "columns": ["element", "qty"],
           "rows": [["Hydrogen", 2], ["Oxygen", 1], ["Carbon", 3]]}

# LATERAL max-population resolve of a colliding city name -> its prominent country (Paris->France, Berlin->Germany).
_CITY_COUNTRY = ('JOIN LATERAL (SELECT country FROM world."Cities" wc WHERE lower(wc.name)=lower({c}.city) '
                 'ORDER BY population DESC NULLS LAST LIMIT 1) w ON true')

CASES = [
    {"label": "sales_by_continent", "cap": "country->continent | group+sum", "tables": [SALES],
     "question": "total amount by continent",
     "oracle": 'SELECT w.continent, SUM(s.amount) FROM sales s '
               'JOIN world."Countries" w ON lower(s.country)=lower(w.name) GROUP BY w.continent'},

    {"label": "top_continent", "cap": "country->continent | argmax", "tables": [SALES],
     "question": "which continent has the highest total amount",
     "oracle": 'SELECT w.continent FROM sales s JOIN world."Countries" w ON lower(s.country)=lower(w.name) '
               'GROUP BY w.continent ORDER BY SUM(s.amount) DESC LIMIT 1'},

    {"label": "total_in_asia", "cap": "country->continent | filter+sum (scalar)", "tables": [SALES],
     "question": "total amount in Asia",
     "oracle": "SELECT SUM(s.amount) FROM sales s JOIN world.\"Countries\" w ON lower(s.country)=lower(w.name) "
               "WHERE w.continent='Asia'"},

    {"label": "countries_in_europe", "cap": "country->continent | filter+count (scalar)", "tables": [SALES],
     "question": "how many are in Europe",
     "oracle": "SELECT count(*) FROM sales s JOIN world.\"Countries\" w ON lower(s.country)=lower(w.name) "
               "WHERE w.continent='Europe'"},

    {"label": "amount_by_currency", "cap": "country->currency | group+sum", "tables": [SALES],
     "question": "total amount by currency",
     "oracle": 'SELECT w.currency, SUM(s.amount) FROM sales s '
               'JOIN world."Countries" w ON lower(s.country)=lower(w.name) GROUP BY w.currency'},

    {"label": "avg_atomic_mass", "cap": "element->mass | avg (scalar)", "tables": [SAMPLES],
     "question": "average atomic mass",
     "oracle": 'SELECT AVG(w.mass) FROM samples s JOIN world."Elements" w ON lower(s.element)=lower(w.name)'},

    {"label": "total_amount_france", "cap": "city->country | filter+sum, 2-table (scalar)",
     "tables": [CUSTOMERS, ORDERS],
     "question": "total amount in France",
     "oracle": "SELECT SUM(o.amount) FROM customers c JOIN orders o ON o.customer=c.name "
               + _CITY_COUNTRY.format(c="c") + " WHERE lower(w.country)='france'"},

    {"label": "count_customers_france", "cap": "city->country | filter+count (scalar)", "tables": [CUSTOMERS],
     "question": "how many customers are in France",
     "oracle": "SELECT count(*) FROM customers c " + _CITY_COUNTRY.format(c="c")
               + " WHERE lower(w.country)='france'"},
]
