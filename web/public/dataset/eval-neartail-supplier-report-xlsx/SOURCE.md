# Formatted supplier-payment report

Original, unmodified Ofgem workbook, June 2026, downloaded 2026-09-12.
Source: https://www.ofgem.gov.uk/about-us/how-we-work/finances/payments-over-25000
File: https://www.ofgem.gov.uk/sites/default/files/2026-09/Payments-to-Suppliers-over-25%2C000-for-June-2026.xlsx

Contains a presentation title, blank rows, report metadata, formatted dates and
amounts, and the real header on row 11. Gold is independently calculated from
Sheet1!G12:G41 (integer pennies), not from an engine answer. No rows are sampled.
Use case: Neartail supplier payments and spend reconciliation. Evaluation only;
not added to examples or training. This does not assert absence from base-model pretraining.

The importer must identify 30 records and preserve numeric values. It must not
interpret the title or report date as a record. Reuse subject to Ofgem's published
copyright policy; retain source attribution.
