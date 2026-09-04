# Prereasoner Website Copy

This is the approved copy source for the separate Prereasoner marketing website. It lives in this
repository so product language, implementation, and evidence can be reviewed together.

The website source is in the separate FormFacade repository. This file does not authorize edits or
deployment there. Copy should be applied there deliberately by the website owner.

## Writing Rules

- Lead with the task and the benefit.
- Use short sentences and concrete verbs: ask, match, join, calculate, check, read, and show.
- Say what the user can see. Prefer `query`, `formula`, `matched rows`, and `source` to abstract terms.
- Make comparisons fair. State what Excel Copilot or Gemini does well before explaining where Prereasoner differs.
- Do not promise perfect accuracy, no AI, no embeddings, real-time source data, add-on behavior, or organization-wide sharing unless the implementation and release evidence support it.
- Keep compliance claims tied to the actual deployment, privacy policy, and service scope.

## Homepage

### Heading

AI built from named dimensions.

### Supporting copy

Attach a Google Sheet, Excel file, or CSV. Prereasoner maps the question and your data to named
dimensions, then compiles them into a checked derivation. Today that derivation runs as typed SQL,
so every dimension can be combined, checked against real rows, and rerun. The same semantic layer
connects tables to public knowledge, private references, and domain-specific calculations.

### Short feature list

- **See the query** - Read the SQL that produced the result.
- **Check the rows** - Open the matched input and reference rows.
- **Use public facts** - Join approved source data when your table does not contain the answer.
- **Keep the calculation** - See currency, ratio, tax, and commission operands and units.
- **Repeat the answer** - Fixed data and model files produce the same result again.
- **Refuse when the data is not enough** - An unsupported answer is returned for review instead of silently filled in.

### Call to action

Try a question with your spreadsheet

## Excel Copilot

### Heading

Copilot for Excel that shows the dimensions behind the calculation.

### Supporting copy

Ask a workbook a question in plain English. Prereasoner maps the question to named dimensions,
matches the right columns, and returns the answer with the query and source rows that produced it.

### What it is for

Use it when the number matters more than a quick suggestion: totals, grouped results, lookups,
date filters, currency conversion, and joins between sheets.

### Prereasoner and Microsoft 365 Copilot

Microsoft 365 Copilot is a broad Excel assistant. It can edit worksheets, generate formulas, create
charts and PivotTables, format data, build lookups, and explain formulas. It is useful when you want
help changing a workbook or exploring its data. Microsoft says to review and verify generated
insights and formulas.

Prereasoner is narrower. It is for asking a question and checking the answer.

| Common Excel workflow | Prereasoner |
|---|---|
| Ask Copilot to add a formula column | Returns the calculation and the query that ran it |
| Add helper columns to normalize, classify, or convert values | Shows the typed operation and its inputs in the derivation view |
| Build helper tables so a lookup or join can be inspected | Uses the selected reference table and shows the join path |
| Review a suggested formula and decide whether it means the right thing | Rejects an unsupported or ambiguous calculation instead of releasing a number |
| Keep a workbook editable while changing its cells | Keeps the uploaded input separate from the read-only derivation and result views |

This is not a claim that Excel Copilot cannot do these tasks. Excel Copilot can create formula rows,
formula columns, and lookups. The difference is the default output: an editable workbook change on
one side, and a checked query with its input path on the other.

### Steps

1. Upload the workbook.
2. Ask the question.
3. Read the result and the query.
4. Open the matched rows or derivation view when you need to check it.

## Sheets Copilot

### Heading

Ask your Google Sheet a question. Read exactly how it answered.

### Supporting copy

Import a Google Sheet and ask about the data in plain English. Prereasoner shows the result, the
matched rows, and the query used to calculate it. The original Sheet is not silently changed.

### Short feature list

- **Use the sheets you already have** - Import the tabs needed for the question.
- **Join related tabs** - Match keys across tables instead of relying on similar wording.
- **Keep the inputs clear** - Input views remain separate from derived results.
- **Read the calculation** - See filters, joins, groups, and arithmetic.
- **Check before sharing** - Review the result and its source path.

### Prereasoner and Gemini in Sheets

Gemini in Google Sheets can create tables and formulas, analyze data, make charts, and apply actions
such as filters, sorting, formatting, and PivotTables. That makes it a useful general assistant for
working inside a Sheet.

Prereasoner is for a different moment: you have a question and want a result you can inspect. It
does not replace the Sheet's formulas or editing tools. It adds a query and source-row view for the
answer it computes.

## Structured RAG

### Heading

Join the right rows. Do not guess from similar text.

### Supporting copy

Link a text column to a public or private reference table with a key. Prereasoner resolves the value,
checks the relationship, and joins the rows needed for the answer.

### Example

Your table contains cities. You ask for sales in France. A similarity search may include Kehl because
it is close to Strasbourg. A key-based join keeps Kehl out because it belongs to Germany. The query
shows that decision.

### How it works

Type, link, join, return.

Prereasoner can use uploaded tables, user-owned reference tables, and approved source releases. A
reference table is selected only when the relationship graph connects it to the request. Unrelated
private references are not added to the query.

## Interpretable AI

### Heading

A confidence score is not a reason.

### Supporting copy

Prereasoner returns the named inputs, the query, and the source path behind a result. A reviewer can
run the same request against the same data and inspect what changed when the result changes.

### Short feature list

- **Named inputs** - See which columns, values, and source facts were used.
- **Repeatable results** - Fixed inputs and artifacts produce the same plan and result.
- **Source-aware answers** - Read the source release and matched reference rows.
- **Clear limits** - Missing or ambiguous evidence produces a reviewable refusal.
- **Local deployment** - Run the engine with your own data and infrastructure.

## Community Edition

### Heading

Run the Prereasoner engine on your own data.

### Supporting copy

The Community Edition includes the Apache-2.0 engine, public runtime weights, and the code used to
build the shared reference tables. Run it on your CPU or deploy it to your own Google Cloud project.

### What is included

- Apache-2.0 source code;
- public, hash-verified runtime weights;
- typed SQL planning and calculation checks;
- source synchronization code and release metadata; and
- a guided Google Cloud deployment.

### Be precise about the database

The source checkout does not contain a ready-made production database snapshot. A deployment builds
the required source tables through the documented ETL and records the source releases used. The
deployment needs a Google Cloud project, authorization, and billing. It is not an anonymous or
zero-cost hosted service.

## Product Navigation

Use these short labels in the product menu:

| Product | Label |
|---|---|
| Prereasoner | Auditable answers from your data |
| Excel Copilot | Ask Excel. Read the calculation. |
| Sheets Copilot | Ask a Sheet. Check the result. |
| Structured RAG | Join rows by key, not similarity |
| Community Edition | Run the engine on your own data |

## Evidence Links

Keep these links near claims that depend on implementation details:

- [Architecture](ARCHITECTURE.md)
- [Deterministic SQL planner](SQL_AST.md)
- [Calculations](CALCULATIONS.md)
- [Source data catalog](SOURCE_DATA.md)
- [Open-source release guide](OPEN_SOURCE_RELEASE.md)
- [Marketing website review](MARKETING_WEBSITE_REVIEW.md)

The Excel comparison is based on the current Microsoft documentation for [Copilot in Excel data
insights](https://support.microsoft.com/en-us/excel/copilot/data-insights-with-copilot-in-excel) and
[getting started with Copilot in Excel](https://support.microsoft.com/en-us/EN-US/excel/copilot/get-started-with-copilot-in-excel).
The Sheets comparison is based on Google's documentation for
[Gemini in Google Sheets](https://support.google.com/docs/answer/14356410?hl=en).
