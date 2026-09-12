# Evaluation source

- Source: UCI Machine Learning Repository, Bank Marketing, dataset 222.
- Downloaded artifact: `bank.csv` from the nested `bank.zip` in `bank+marketing.zip`.
- License: Creative Commons Attribution 4.0 International (CC BY 4.0).
- URL: https://archive.ics.uci.edu/dataset/222/bank%2Bmarketing
- Transformation: the source semicolon-delimited CSV was re-serialized as UTF-8 comma-delimited CSV; `contact` was renamed to `contact_method`, `y` to `subscription_status`, and its `yes`/`no` values to `subscribed`/`not_subscribed` so the fixture exposes business terminology. The release questions intentionally cover aggregate field operations without depending on boolean-value synonym resolution. Other values and rows were not changed.
- Purpose: FormFacade-style lead and campaign-response evaluation only; not a home-page example and not a training corpus.
