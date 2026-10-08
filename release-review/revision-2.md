# Release review — revision 2

| Review identity | Value |
|---|---|
| Date | 2026-10-07 |
| Repository | C:/work/prereasoner-data |
| Reviewed HEAD | 5f8c57c03674e96a4baf55edc7ae5db98775cfb4 |
| Previous baseline | 106aefc4753d740ebed75220ce4c272fddb244d2 |
| Output | C:/work/prereasoner-data/release-review/revision-2.md |

## 1. Assessment

**Everything is not yet fine.** Claude's changes address the original decimal-comma corruption and large-integer join-grounding failures. They also improve negation, case-sensitive joins, geographic routing, geographic tie-breaking, and evaluator source provenance. Those improvements are real, but several fixes cover only part of their failure family.

Of the previous report's 21 correctness findings:

- **2 are resolved for the originally reported defect:** B02 and B13.
- **3 are partially resolved:** B04, B07, and B11. New counterexamples are documented below.
- **16 remain open:** their relevant behavior remains present in the current owners.

This revision documents **7 additional findings or counterexamples**. N01, N03, and N04 deepen those three partial fixes; N02, N05, N06, and N07 are additional issues beyond the original list. “Newly found” does not mean “introduced by this release.” Each finding explains that distinction.

The most pressing additional failures are:

1. Mixed text/numeric key columns can be certified as a complete relationship while the executed join drops most source rows.
2. Spreadsheet percentage formatting is lost, allowing a displayed 20% tax to be applied as 0.2%.
3. The geographic shortcut still ignores non-Latin subjects and accepts place categories it does not actually constrain.
4. A capped saved reference can produce an incomplete aggregate without warning.

Existing P1 concerns also remain: destructive text parsing, false equality certification, literal substring corruption, stale FX validity, stale answer provenance, the upload/execution transaction gap, and legitimate rows classified as totals.

**Release recommendation:** do not treat passing fixtures or unchanged Spider answers as proof that the affected calculation and provenance paths are correct. Resolve the P1 cases, add the specific regressions below, then evaluate through the existing serving owners. This report changes no production behavior.

### Evidence conventions

- **Reproduced:** observed by invoking production helpers, search, guards, or execution locally. Any dependency substitution is stated.
- **Static:** source inspection establishes the failure path; the corresponding live database, installation interruption, or release was not exercised.
- **Opportunity:** a cost or limitation exists, but no production improvement size is claimed.
- **P1:** correct before relying on the affected path for accurate results.
- **P2:** meaningful correction or optimization, following the P1 work.

A constructed wrong AST accepted by a guard proves a guard defect. It does not prove that the ordinary search selects that AST for every real request. Conversely, executing a query successfully proves neither its meaning nor its source completeness.

Code locations below are absolute plain-text references. The first report's clickable file links with line suffixes break this repository's documentation-link check; this report avoids adding that failure.

## 2. Scope, changes, and verification

I read the repository rules, the supplied Claude activity transcript, the earlier report, the intervening changes, and the current implementations and callers. The diff from the first baseline spans 25 files, with 602 additions and 135 deletions.

The two relevant commits are:

- b429454: negation polarity, geographic intent gating and ordering, comma grouping, exact/case-sensitive key comparison, browser CSV preservation, and regressions.
- 5f8c57c: evaluator fingerprints every Python source under engine/ and spider/probe/, replacing a manually maintained list.

The review covers ingestion and workbook normalization; typed AST search, schema, grounding and completeness; calculation specifications and both deterministic execution paths; world/compose routing and FX maintenance; request budgets, replay, persistence and provenance; browser workbook lifecycle and Excel/Sheets integration; orchestration and MCP contracts; model installation and release/evaluation evidence.

This is a broad first-party review with focused adversarial checks, not an exhaustive audit of vendor libraries, every model weight, every external dataset row, or real Office/Apps Script implementations.

### Checks performed on the reviewed checkout

| Check | Result | Interpretation |
|---|---|---|
| Python: .venv/Scripts/python.exe -m tests.run_all, with RUN_ENGINE_TESTS=0, RUN_ORCHESTRATOR_TESTS=0, EXTERNAL_LLM_ENABLED=0 | **37 of 38 invoked suites passed; tests.test_release failed** | Applicable local suites ran; live suites were deliberately excluded. |
| tests.test_release within that run | **40 passed, 1 failed** | The sole failure is test_local_documentation_links_resolve, caused by 69 links in the earlier untracked report. |
| npm run test:web | **All 11 test programs passed** | Browser import/lifecycle, Excel integration fixtures, hosting fixtures, and Sheets logic. |
| npm run test:browser | **65 passed in 51.9 seconds** | Local browser fixtures, not deployed production or a real Excel host. |
| Python compileall over engine, db, deploy, training, tests, orchestrator, mcp_server, regress, spider, world_eval | **Passed** | Source compilation. |
| Ruff over those areas with F and E9 selected | **Passed** | Selected name/import and syntax checks, not a full type/style audit. |
| Targeted production-helper, AST, import, and local execution probes | **Failures and positive controls recorded below** | Reproductions extend beyond the existing fixtures. |

The earlier report is C:/work/prereasoner-data/release-review/.md. I introduced its incompatible absolute line-number hyperlinks in the first review. The failure is a documentation artifact issue, not a new engine regression. It also means the first report's green Python statement describes the run before that report was added, not the final workspace. I preserved that earlier file in this task; its SHA-256 remains BE94048F578105D765C7A6EE615F9E3F30D7F8EC8B36731CC90385F48F8B1B19. Repair its links before claiming a completely green local release gate.

The excluded live engine suites were tests.test_world, tests.test_nongeo, tests.test_world_joins, tests.test_route_wired, tests.test_geo, tests.test_schema_probes, tests.test_datasets, tests.test_question_families, tests.test_scale, tests.test_request_replay_live, and tests.test_pg_deadline_live. The live tests.test_orchestrator suite was also excluded.

No fresh full Spider run, paid-model request, production deployment, model promotion, training, or live database concurrency test was performed in this review.

### Claude's supplied release evidence

The supplied transcript reports b429454 deployed as engine prereasoner-api-00157-vfh and chat prereasoner-chat-00112-6vb, with matching Hosting assets. It reports 102/102 fresh-conversation replies and 79/79 existing-conversation replies across 24 datasets, all 181 identical to the prior release, plus production synthetic checks of the original negation, geographic interception, and decimal-comma examples.

These are useful supplied observations, **not independently rerun production checks in this review**. The transcript also reports one 96-second reply, with 87 seconds in the chat's first Gemini call and approximately 8.5 seconds in the engine. One observation does not establish a regression or its frequency. Account storage figures in the transcript are historical observations, not verified current quota readings.

## 3. Status of every earlier correctness finding

| ID | Original concern | Current status | Current evidence and disposition |
|---|---|---|---|
| B01 | Leading zeros and literal wrapping quotes erased | **Open — P1** | Reproduced again: 001 and 1 both become 1; literal 'Paris' becomes Paris. Parsing still precedes role/type decisions. |
| B02 | Decimal commas silently magnified | **Resolved for the reported corruption** | 1,50 and 1.234,56 now fail numeric parsing and remain text; valid western/Indian grouping parses. Locale-aware arithmetic remains an opportunity, not a completed feature. |
| B03 | Equality accepted without proving equality | **Open — P1** | Reproduced again: “Amount equals 10” with Amount > 10 has no violations and complete coverage. |
| B04 | Any exclusion substitutes for the requested exclusion | **Partial** | Original raw-column swap is rejected and ordinary positive controls work. Wrapped operands still bypass polarity checks; see N04. |
| B05 | Top/bottom check ignores direction | **Open — P2** | Reproduced again: “top 1 ... by Amount” with ascending Amount and LIMIT 1 is certified complete. |
| B06 | Literal substrings become LIKE wildcards or a different literal | **Open — P1** | Reproduced again: a_b matches acb; asking for a%b also proposes the a_b pattern on the fixture. |
| B07 | FK discovery disagrees with emitted equality | **Partial — P1** | Case-only false matches are addressed. Numeric canonicalization still disagrees with mixed TEXT storage; see N01. |
| B08 | Rebuild date extends old FX validity | **Open — P1** | Reproduced again: January 1 observation rebuilt February 1 remains valid February 8. |
| B09 | Old answer snapshot relabeled with newest source hash | **Open — P1** | Static recheck: save_state still substitutes the current conversation sourceHash into the supplied snapshot. |
| B10 | Deterministic snapshot begins after upload lock is released | **Open — P1** | Static recheck: upload transaction commits/closes before a separate execution connection begins REPEATABLE READ. |
| B11 | Nearby shortcut intercepts uploaded-data questions | **Partial — P1** | Original English controls pass. Non-Latin subjects and unsupported place categories still enter the shortcut; see N03. |
| B12 | Decimal division rounding differs between execution paths | **Open — P2** | Reproduced again: 1/2097152 rounds differently in SQLite decimal helpers and deterministic DIVIDE. |
| B13 | Float normalization merges distinct exact integer keys | **Resolved for the reported collision** | Shared join_value keeps 9007199254740992 and 9007199254740993 distinct; the float-based grounding helper is removed. N01 is a separate type-context gap. |
| B14 | Conversational pre-gate misses non-Latin schema mentions | **Open — P2** | Static recheck: ASCII question tokens and ASCII schema splitting remain. Separate from the nearby bypass in N03. |
| B15 | Real entity named Total classified as summary | **Open — P1** | Reproduced again: company Total, amount 20 leaves the main table and becomes a summary row. |
| B16 | Empty Excel document URL produces new workbook identity | **Open — P2** | Static recheck: workbookKey still creates a random UUID when URL is empty; task-pane state is in memory. |
| B17 | Connection retries reuse original remaining budget | **Open — P2** | Reproduced with mocked transport failures: all three connect_timeout values are 2 despite decreasing available budget. |
| B18 | Local runtime identity hashes empty engine/engine directory | **Open — P2** | Reproduced again without deployment identity variables: source-sha256 is the canonical empty-map hash. |
| B19 | Zero FK confidence becomes full confidence | **Open — P2** | Reproduced again: conf=0.0 becomes ForeignKey confidence=1.0. |
| B20 | Runtime bundle replacement atomic only per file | **Open — P2** | Static recheck: promotion/fetch replace individual files; interruption/failure between them can leave mixed state. |
| B21 | Release gate incompletely binds source/model/hosted evidence | **Open — P2** | Static recheck: build verifies a seven-character tag, not full source/model identity; hosted lanes can pass with summary status alone. |

### What the fixes demonstrably improve

The numeric parser now validates commas against one shared grouping grammar. Both invalid examples from B02 are preserved instead of converted to a wrong magnitude. Valid 1,234.56 and 12,34,567 remain supported. This is appropriate conservative behavior when the numeric locale is unknown; arithmetic on decimal-comma text still requires a legitimate format contract.

The ordinary “orders not Done in France” path now keeps France and excludes Done. The normal planner also excludes both Done and Cancelled for “not Done or Cancelled” on the checked fixture; a wrong OR candidate appearing in the pool is not itself a served failure when selection rejects it.

Exact integer keys no longer pass through float normalization in grounding. Text case is retained, so ABC and abc no longer imply an equality relationship.

The nearby query now requires both latitude and longitude, orders by full distance before rounding, and uses stable entity/name tie-breakers. The shortcut also invokes calculation verification before returning. Those are useful changes, though they do not prove the requested subject or place category.

The evaluator source scan now returns 116 Python files and includes engine/relations.py, engine/numeric.py, and spider/probe/evalutil.py. The manual source lists are removed. Non-source model/data fingerprint omissions remain, as described in N05.

## 4. Additional findings and partial-fix counterexamples

### N01 — P1: canonical numeric keys do not match mixed TEXT join storage

**Evidence: reproduced through FK discovery and the existing production TableQuery.serve path with the repository's hermetic encoder substitute.** This is the remaining B07 family, and the new numeric canonicalization permits this particular false relationship.

**Locations:** C:/work/prereasoner-data/engine/relations.py:82; C:/work/prereasoner-data/engine/relations.py:140; C:/work/prereasoner-data/engine/tables.py:360; C:/work/prereasoner-data/engine/tables.py:752; C:/work/prereasoner-data/engine/sql_grounding.py; C:/work/prereasoner-data/engine/master.py:212.

Reproduction input:

~~~csv
# orders
customer_id,amount
1.0,10
1.0,20
2.0,30
ABC,40

# customers
customer_id,name
1,Alice
2,Bob
ABC,Chris
~~~

The ordinary CSV parser creates Decimal values on the first table and integers on the second, alongside ABC. The mixed key columns are TEXT. Their executed values are therefore '1.0'/'2.0' and '1'/'2', respectively.

join_value canonicalizes the numeric-looking values without the column's storage type. FK discovery reports confidence=1.0 and inclusion=1.0. Grounding uses that same context-free normalization.

~~~text
Question: total amount by customer name
Selected SQL:
  SELECT customers.name, SUM(orders.amount)
  FROM orders JOIN customers
    ON orders.customer_id = customers.customer_id
  GROUP BY customers.name

Returned rows: [['Chris', 40]]
clarify: None
error: None
~~~

The apparent complete relationship does not match execution: three numeric-key fact rows disappear. If these are genuinely distinct textual identifiers, claiming full inclusion is wrong. If the product intends numeric equivalence, the emitted equality does not implement that equivalence. Either interpretation requires the inference and execution contracts to agree.

**Correction:** make key comparison depend on the actual column storage/typed join semantics. Use that one contract for target uniqueness, FK discovery, grounding, reference selection, SQL rendering and Python execution. Do not normalize individual numeric-looking cells in a mixed TEXT column unless the emitted typed join explicitly implements an authorized conversion. Correct B01's irreversible identifier coercion at the same source boundary.

**Acceptance:** mixed alphanumeric keys, numeric-looking TEXT keys, Decimal/integer joins, scientific notation, leading zeros, case differences, blanks, composite keys and large exact integers. Assert inclusion/uniqueness and executed row retention, not only discovery. Run PostgreSQL and both deterministic emitters.

**Limit:** the probe substitutes encoder readings and makes no claim about frequency with the promoted model. Production storage selection confirms the mixed TEXT premise.

### N02 — P1: percentage formatting is discarded before rate calculation

**Evidence: reproduced through the actual shared XLSX conversion, the live-grid conversion contract, RateApplicationSpecification, typed search, and local SQL execution.** This is newly identified, pre-existing behavior.

**Locations:** C:/work/prereasoner-data/web/public/lib/workbook-import.js:24; C:/work/prereasoner-data/web/public/lib/workbook-import.js:136; C:/work/prereasoner-data/web/public/lib/workbook-import.js:150; C:/work/prereasoner-data/engine/calculations/specifications.py:565; C:/work/prereasoner-data/engine/calculations/specifications.py:674.

Create an XLSX worksheet:

~~~text
price | tax_percent
100   | 20%

Underlying B2 value: 0.2
B2 number format: 0%
Question: total price including tax
~~~

The shared reader has the number format, but extracts raw values and exports:

~~~csv
price,tax_percent
100,0.2
~~~

Its import metadata records field names and original headers, with no percentage unit and no warning. Passing the same values and formats through convert({grids: ...}) gives identical output, so this affects the common import boundary used by host grids as well as files.

_rate_scale sees “percent” in the column name and applies a divisor of 100:

~~~text
Typed expression: SUM(price * (1 + tax_percent / 100))
Executed result: 100.2
Expected for the displayed 20% rate: 120
~~~

The rate contribution is 100 times too small. Matching SQL/Python answers would still certify the wrong business meaning, because both derive from the same lost unit.

**Correction:** preserve unambiguous percentage-storage semantics at the existing shared import boundary and carry them into the existing rate specification. Normalize a percentage-formatted stored fraction to a declared percent value, or retain explicit fraction metadata consumed by the typed plan. Keep one importer and one rate planner. Magnitude alone cannot decide whether plain 0.2 means 0.2% or 20%.

**Acceptance:** numeric 0.2 with 0% formatting; numeric 20 in an explicitly percent-valued column; explicit fraction columns; text 20%; mixed/unknown formats; blank cells; XLSX, CSV, Sheets grids and Excel grids. Verify the interpreted unit and actual computed amount across SQL/Python, not only imported CSV equality.

**Limit:** local workbook bytes and host-shaped grids were exercised. A real Office or Apps Script host and a complete promoted-encoder request were not used for this fixture.

### N03 — P1: nearby intent gating still loses subjects and category constraints

**Evidence: reproduced through the new gate and KnowledgeReasoner._serve with only the geographic lookup/result dependencies substituted.** This is incomplete coverage of B11, not a claim that the original English examples remain broken.

**Locations:** C:/work/prereasoner-data/engine/knowledge.py:39; C:/work/prereasoner-data/engine/knowledge.py:63; C:/work/prereasoner-data/engine/knowledge.py:151; C:/work/prereasoner-data/engine/knowledge.py:173.

The gate reads only ASCII letters before the nearness phrase. A non-Latin subject yields no tokens; all(empty) is True.

~~~text
orders near Paris       -> geographic gate False
sales around Christmas  -> geographic gate False
売上 near Paris          -> geographic gate True  (sales)
订单 near Paris          -> geographic gate True  (orders)
capitals near Paris     -> geographic gate True
~~~

For the two non-Latin subjects, _serve returns the supplied geographic result before consulting the uploaded table path. The substituted lookup demonstrates the routing branch; it does not pretend to validate live settlement rows.

There is a second semantic gap: capital/capitals and village/villages are allowed, but the emitted settlement query constrains coordinates, population, names, and administrative exclusions, not capital status or settlement category. “capitals near Paris” can therefore return ordinary nearby cities. “Smallest” is likewise admitted without a corresponding size ordering.

**Correction:** bind the geographic subject, requested category, ordering and source scope to a typed intent that the shortcut can actually prove. Preserve Unicode subjects. Delegate unsupported own-data or category requests to the existing composed path or provide a bounded clarification. Expanding an English allowlist is not sufficient; an admitted noun/modifier must correspond to an executable predicate or ordering.

**Acceptance:** original English positives and negatives; Unicode upload names and subjects; capitals versus ordinary cities; villages versus cities; smallest versus closest/largest; count/radius requests; subjects on either side of the nearness clause; named upload coordinate columns. Assert route and meaning, not just a nonempty result.

### N04 — P2: wrapped text operands bypass the new negation polarity guard

**Evidence: reproduced with a constructed valid typed AST and the actual completeness policy.** B04's original raw-column counterexample is fixed; the guard remains incomplete.

**Location:** C:/work/prereasoner-data/engine/query_contract.py:365, particularly the ColumnRef/Literal-only loop at lines 381–390.

~~~text
Question: orders not Done in France

Candidate:
  WHERE LOWER(status) = 'done'
    AND country = 'France'
    AND Amount != 0

constraint_violations: ()
coverage.complete: True
~~~

The generic exclusion check is satisfied by Amount != 0. The new polarity check skips LOWER(status), so the AST can explicitly keep the value the question excludes and still be certified complete.

LOWER is an existing typed expression, not malformed SQL. Restricting the polarity guard to raw ColumnRef operands leaves transformed predicates outside the proof.

**Correction:** normalize supported typed expressions to their base operand and comparison semantics when validating requested polarity, and require the requested exclusion itself. Account for literal case transformations, mandatory predicates, membership and existential scope. Unknown transformations should not silently prove coverage.

**Acceptance:** wrong/omitted negation under LOWER; multiple positive and negative filters; NOT LIKE; NOT IN/NOT EXISTS; reversed operands; AND versus non-mandatory OR; genuine values “No” and “Not Started”; aliases and nested scopes. Check both rejection of wrong candidates and retention of correct ordinary candidates.

**Limit:** this is a coverage counterexample, not evidence that the normal search currently ranks this constructed query first.

### N05 — P2: evaluator checkpoint identity still omits model and database inputs

**Evidence: static checkpoint-contract inspection, plus a direct call of the new _source_paths owner.** The source-scan fix works. This is an acknowledged remaining provenance gap rather than a regression from that change.

**Locations:** C:/work/prereasoner-data/spider/probe/full_eval.py:146; C:/work/prereasoner-data/spider/probe/full_eval.py:633; C:/work/prereasoner-data/spider/probe/full_eval.py:648; C:/work/prereasoner-data/spider/probe/full_eval.py:669; C:/work/prereasoner-data/engine/encoder_overlay.py:32; C:/work/prereasoner-data/engine/artifact_provenance.py:151.

The checkpoint contract hashes dev.json, tables.json, encoder.pt, encoder_meta.pt, the adapter, and all engine/probe Python source. It does not hash the runtime weights manifest, anchor_assignment.npz, other manifested threshold/model assets, or the SQLite database contents loaded from args.dbs.

anchor_assignment.npz is actually read by the encoder loader and determines firing thresholds. A legitimate coherent bundle update can change such an asset and its manifest while leaving the two hashed encoder files and adapter unchanged. A dirty-worktree Boolean cannot distinguish two different dirty trees, and gitignored artifacts have no commit identity.

Database contents can also change while dev.json and tables.json stay identical. load_capped then changes the tables the planner executes, but --resume compares the old contract and may reuse old predictions.

Manifest validation does protect against unmanifested corrupt bytes. It does **not** make two different valid bundles the same prediction input, nor does it identify database contents.

**Correction:** use the existing validated complete-bundle fingerprint and fingerprint the actual selected database inputs. Include relevant effective execution settings in the same checkpoint contract. Preserve the single evaluator and fail-fast resume comparison.

**Acceptance:** unchanged inputs resume; edits to each executed source invalidate; coherent threshold/manifest updates invalidate; a changed SQLite row invalidates even when schema/labels stay unchanged; relocation of byte-identical data remains valid if desired. Test with temporary small databases and bundles, not production model replacement.

**Limit:** no actual weights or Spider databases were modified, and no full resumed model run was undertaken for this review.

### N06 — P2: grouped numeric thresholds are lost in question tokenization

**Evidence: reproduced through production TableQuery.serve with the existing hermetic encoder substitute.** This is a newly identified planner limitation, not established as a regression from the comma fix.

**Locations:** C:/work/prereasoner-data/engine/sql_expansion.py:1008; C:/work/prereasoner-data/engine/sql_expansion.py:1017; C:/work/prereasoner-data/engine/sql_expansion.py:1022; C:/work/prereasoner-data/engine/numeric.py:30; C:/work/prereasoner-data/engine/compose.py:275.

~~~text
orders Amount values: 2, 999, 1001, 2000

orders with Amount over 1000
  -> WHERE Amount > 1000
  -> rows 1001, 2000

orders with Amount over 1,000
  -> planner: no executable AST candidate
~~~

The shared numeric grammar accepts 1,000, but the question tokenizer splits it into separate 1 and 000 tokens. Consequently, the typed numeric-comparison expansion never receives the number in the form the parser accepts. Updating NUMBER_TEXT in search/schema does not repair the earlier loss.

Compose now has a grouped-number regex, increasing the importance of consistent interpretation at the common question boundary. It should not become an alternate own-data solver for the AST miss.

**Correction:** preserve valid numeric spans, including grouped digits, signs and decimal punctuation, in the existing tokenizer/expansion contract. Numeric spans must use the shared validated grammar and retain source positions.

**Acceptance:** 1000 and 1,000 select equivalent plans/results; western and Indian grouping; decimals and negative values; numbers followed by clause punctuation; invalid decimal commas; dates, limits and numbers embedded in identifiers. Check original-request coverage after any permitted wording rewrite.

### N07 — P1: reference truncation can silently remove source rows from aggregates

**Evidence: reproduced through actual relevant_tables selection and TableQuery.serve, with the private persistence reads mocked to return a legal stored reference.** Newly identified pre-existing behavior.

**Locations:** C:/work/prereasoner-data/engine/server.py:135; C:/work/prereasoner-data/engine/server.py:811; C:/work/prereasoner-data/engine/master.py:177; C:/work/prereasoner-data/engine/master.py:238; C:/work/prereasoner-data/engine/master.py:262.

Private references support 50,000 stored rows, while request-time inclusion is capped at 5,000. The catalog reads the first keys in key order; the selected full table is then sliced to the request cap, with no truncation disclosure or matched-key retrieval.

Fixture:

~~~text
Stored products: 5,001 unique keys K00000 ... K05000
Category for first 5,000 keys: included
Category for K05000: tail

Orders: keys K00000 ... K00099 and K05000, each appearing twice
Amount per order: 1
Source rows: 202

Selected reference rows: 5,000
Reference warnings: []

Question: total amount by category
Returned: [['included', 200]]
Expected with the complete saved reference:
  [['included', 200], ['tail', 2]]
clarify: None
error: None
~~~

The truncated reference still satisfies the discovery threshold on distinct keys, so the inner join proceeds and loses the two tail rows. If enough request keys lie beyond the prefix, the reference can instead be omitted with no truncation explanation.

A row cap is legitimate resource control. Treating an arbitrary prefix as the complete applicable reference changes the meaning of the answer.

**Correction:** retrieve relevant keys within the existing private-reference owner and a bounded request budget, rather than an arbitrary sorted prefix. If coverage cannot be complete, disclose the omission and prevent an unqualified full-total answer. Preserve multi-hop selection, tenant isolation and existing quotas.

**Acceptance:** tail-only keys; a mostly matching prefix plus tail rows; high multiplicity on a rare tail key; 50,000 stored reference rows; chained references; request/table budgets; warning propagation into the final answer. Verify both final totals and retained source-row counts.

**Limit:** the DB reads were mocked; the production selector, discovery, planner and local execution ran unchanged. A live persistence test is still needed.

## 5. Remaining original findings: actionable details

### Source fidelity: B01 and B15

**Locations:** C:/work/prereasoner-data/engine/tables.py:152, :168, :175, :200; C:/work/prereasoner-data/web/public/lib/workbook-import.js:133.

The current parser still maps code values 001 and 1 to identical integers and strips a literal wrapping quote pair after CSV decoding. On the checked fixture, both label values 'Paris' and Paris become Paris. Column-role decisions made later cannot recover those distinctions.

The browser importer still decides that any later row beginning with Total, Subtotal or Grand total is a summary. A legitimate company named Total is moved out of the main table:

~~~text
Input: company,amount / Acme,10 / Total,20
Main CSV: company,amount / Acme,10
Summary CSV: company,amount / Total,20
~~~

There is a generic summary warning, but it certifies an inference that is false for this dataset. At the workbook table limit the summary can be omitted altogether.

**Correct in the existing ingestion owners:** retain raw identifier/text evidence until column semantics are established; avoid decoding quotes twice; require structural/formula evidence for a summary classification, and keep ambiguous rows as data. Test leading zeros, long identifiers, significant whitespace, literal quotes, an entity named Total, real subtotal formulas, and full table-limit workbooks. Percentage units in N02 belong at this same boundary.

### Semantic certification: B03, B05 and B06

**Locations:** C:/work/prereasoner-data/engine/query_contract.py:342; C:/work/prereasoner-data/engine/query_contract.py:582; C:/work/prereasoner-data/engine/query_contract.py:405; C:/work/prereasoner-data/engine/sql_search.py:955; C:/work/prereasoner-data/engine/sql_search.py:1976; C:/work/prereasoner-data/engine/deterministic/operators.py:297.

The equality check still runs only when a requested numeric comparison is not equality. “orders with Amount equals 10” accepts Amount > 10 with no violations and complete coverage. The original exact operand/value/direction proof is still needed.

The top/bottom check verifies a limit and presence of ordering but not the requested direction. “top 1 orders by Amount” accepts ascending Amount. Its guard should bind the ranking measure and direction, not just LIMIT.

Literal substring filters still fail to escape LIKE wildcard characters. On names a_b, acb, a%b, axyb, asking for names containing 'a_b' proposes LIKE '%a_b%', and the existing LIKE operator confirms acb matches. Asking for 'a%b' also proposes '%a_b%' on this fixture because punctuation-insensitive matching loses the literal distinction.

**Correction:** prove requested comparisons and ranking in typed constraints; preserve quoted substring identity; represent literal containment/escaping consistently in the existing AST, renderers, deterministic emitters and coverage. Test wrong operands, numeric equality, grouping/HAVING, AND/OR scope, top/bottom with tied scores, percent/underscore/backslash/apostrophes, Unicode and punctuation-only distinctions. Do not fix these by expanding a noise-word list.

### FX validity: B08

**Location:** C:/work/prereasoner-data/db/sync/build_exchange_rate.py:70, particularly lines 91–99.

The carry horizon remains max(last observation date, rebuild date + seven days). The latest active series is extended to that horizon regardless of genuine publication age.

The current helper again produced a USD row valid 2026-02-08 from a genuine 2026-01-01 rate when rebuilt on 2026-02-01. updated_at correctly remains January 1, but the validity is still 38 days beyond the observation.

**Correction:** constrain non-identity rates by genuine per-series observation dates and the documented carry policy. Rebuilding stale input must not create freshness. Cover weekends/holidays, outages, withdrawn series, historical gaps and missing pair legs. Retain legitimate EUR/EUR identity separately.

### Snapshot and provenance correctness: B09 and B10

**Locations:** C:/work/prereasoner-data/engine/conversations.py:918; C:/work/prereasoner-data/web/public/lib/workbook-conversations.js:261; C:/work/prereasoner-data/engine/pg.py:183, :271, :293; C:/work/prereasoner-data/engine/deterministic/service.py:296; C:/work/prereasoner-data/engine/conversations.py:483.

save_state still replaces a supplied snapshot's sourceHash with the latest stored conversation hash. An old rendered answer arriving after a new source upload can therefore be labeled current, defeating the browser's stale-state comparison. **Static evidence:** the overwrite remains; this review did not rerun a live late-write race.

The deterministic path still commits and closes the upload transaction before creating a separate execution connection. Another request can replace working tables in that gap. REPEATABLE READ makes the two emitters agree with each other after the second snapshot starts; it does not prove that they read the upload used for the original selection. complete_analysis checks conversation-level versioning, which does not establish every working-table hash across request scopes/reference sets.

**Correction:** retain the answered snapshot's source/version identity and reject conflicting late writes; bind execution to the selected source snapshot or carry the upload lock/transaction through execution. Test interleavings with barriers and actual PostgreSQL transactions. Include differing table scopes and reference/enrichment selections at the same conversation dataset version. This is why parity alone is insufficient evidence of source accuracy.

### Numeric parity: B12 and B19

**Locations:** C:/work/prereasoner-data/engine/numeric.py:180; C:/work/prereasoner-data/engine/deterministic/operators.py:256; C:/work/prereasoner-data/engine/sql_schema.py:339.

~~~text
1 / 2097152:
SQLite decimal helper: 0.00000047683715820312
Deterministic DIVIDE:  0.00000047683715820313

Explicit FK conf=0.0:
SchemaGraph confidence: 1.0
~~~

The division paths still use different rounding policies at the scale boundary. The dictionary FK decoder still applies “value or 1.0,” replacing a valid zero.

**Correction:** declare and use one arithmetic precision/scale/rounding policy, including AVG; default confidence only when absent. Test halfway values and negative values in all supported backends, and absent/zero/fractional/one/invalid FK confidence. These are narrow deterministic corrections; no training is required.

### Request and client lifecycle: B14, B16 and B17

**Locations:** C:/work/prereasoner-data/engine/knowledge_compose.py:132; C:/work/prereasoner-data/engine/knowledge_compose.py:153; C:/work/prereasoner-data/web/public/office/excel/host.js:166; C:/work/prereasoner-data/web/public/office/excel/taskpane.js:15, :119; C:/work/prereasoner-data/engine/pg.py:132.

The ordinary conversational pre-gate still tokenizes questions with [a-z]+ and schema names with an ASCII pattern. A non-Latin schema mention without an English data-intent term can be treated as conversational before the planner sees it. Use the existing Unicode lexical contract and test named Unicode tables/fields with positive and genuine meta-chat contrasts.

An Excel document with an empty URL still gets a random workbookKey. A new task-pane instance loses in-memory state and may create a different sheet-session identity for the same document. Use a persistent supported document identity/lifecycle and test reopened panes, unsaved workbooks and multiple documents. A random identifier stable only within one pane is insufficient.

_pg reads remaining budget before its retry loop. A mocked decreasing budget yielded connect_timeout values [2, 2, 2]. Three full two-second transport waits plus current backoff could consume approximately 6.75 seconds against an initial two-second budget; this is a bound from the policy, not a measured live incident. Re-read/enforce the remaining deadline before every retry and before backoff.

### Model and release identity: B18, B20 and B21

**Locations:** C:/work/prereasoner-data/engine/enrichment/runtime.py:42; C:/work/prereasoner-data/training/props/promote.py:118; C:/work/prereasoner-data/training/schema_org/promote.py:349; C:/work/prereasoner-data/engine/fetch_weights.py:166; C:/work/prereasoner-data/deploy/gcp/release_gate.py:26, :49, :63.

RuntimeIdentity.current sets engine_root to the engine directory, then scans engine_root/engine. With neither K_REVISION nor GIT_COMMIT set, the reproduced source identity is the empty-map hash:

~~~text
source-sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
~~~

The planner/ranker hashes are separate and do not restore complete source identity. Scan the actual source tree and fail clearly when expected sources are absent. The defect is conditional on missing external build identity; it does not show current Cloud Run revisions use this fallback.

Promotion/fetching still installs files individually. Fetch validates staged bytes before installation, which is useful, but an interruption between replacements can leave a mixed active bundle. Promotion validates the installed manifest after replacing files. Property promotion also accepts a nonempty revision where fetching expects an immutable 40-character revision. Use prevalidation plus exclusive failure-safe replacement/rollback in the existing owners; test every interruption boundary in temporary directories. No model installation was attempted here.

The release build gate still checks the seven-character _TAG and build/image records without validating complete build-source/model identity against the manifest. verify_launch accepts lane status='passed' summaries, public_examples.total=18, and a recorded Apps Script version without requiring the individual prompt/follow-up evidence and full component identities. Bind the full tested source, bundle, image/deployment identities and per-example outcomes. Test wrong full commits sharing a prefix, wrong model hashes, missing/duplicate prompt records and mismatched hosted assets. This is a gate completeness finding, not an assertion that the supplied deployment was wrong.

## 6. Performance opportunities

The ten original opportunities remain. No production latency improvement was measured in this review. P11 identifies additional work introduced by the case-normalization warning pass.

| ID | Opportunity and location | Proposed change in the current owner | How to measure without compromising correctness |
|---|---|---|---|
| P01 — P2 | C:/work/prereasoner-data/engine/tables.py:581 executes the candidate pool before cheap constraint filtering at :603. | Apply sound structural/semantic rejections before execution; retain execution validity and the current selection owner. | Candidate execution count and request latency on identical pools; selected SQL, recall and coverage unchanged except intended correctness fixes. |
| P02 — P2 | C:/work/prereasoner-data/engine/pg.py:99 sends timeout configuration before each statement, outside the reported SQL span/count. | Reduce configuration round trips where deadline/transaction behavior allows; instrument actual statements through request_timing. | Actual DB statement/round-trip counts versus sql_n/sql_ms; deadlines, rollback and savepoint recovery preserved. |
| P03 — P2 | C:/work/prereasoner-data/engine/request_replay.py:26 opens a connection and performs claim/lock queries every 250 ms while another caller owns the job. | Bounded backoff or existing-owner notification/pooling, while preserving durable cross-instance ownership. | Duplicate callers, DB QPS/connections, replay latency and lease recovery under contention. |
| P04 — P2 | C:/work/prereasoner-data/engine/deterministic/runtime.py:150 materializes rows from every SQL stage; service later shows first 50 non-output rows. | Bound non-output trace retrieval when parity does not require full rows; avoid repeated view recomputation where one DAG can materialize needed stages. | Peak memory, rows fetched and stage execution time on large joins; complete output and real parity checks retained. |
| P05 — P2 | C:/work/prereasoner-data/engine/server.py:132 bounds parsed-cache cells, not bytes/entries; eviction recomputes total cells. | Add byte/entry accounting and constant-time maintained totals in this cache owner. | Memory under long text and zero-row/header-only entries, cache hit rate and eviction cost; ownership-scoped keys preserved. |
| P06 — P2 | C:/work/prereasoner-data/engine/relations.py:54 rebuilds full row tuples for memo lookup keys. | Use request-local immutable table identity/content fingerprints established once at normalization. | Memo-hit cost versus cell count; invalidation after every relevant mutation and cross-request isolation. |
| P07 — P2 | C:/work/prereasoner-data/engine/numeric.py:207 stores every Decimal operand for SUM/AVG/MIN/MAX. | Maintain running aggregate state with the declared precision/rounding policy. | Aggregate peak memory and execution time; adversarial decimal results identical after the B12 policy fix. |
| P08 — P2 | C:/work/prereasoner-data/engine/trace.py:78, :101 ordinary trace writes remain synchronous. | Coalesce bounded nonterminal trace updates through the existing transport owner; preserve terminal delivery ordering. | Trace time/count with slow RTDB, terminal result reliability and disconnect recovery. |
| P09 — P2 | C:/work/prereasoner-data/web/public/office/excel/host.js:12, :119 reads in 200-row sync chunks and converts grids through XLSX bytes; worker has no timeout. | Measure host chunking and conversion cost; tune bounded reads and share worker lifecycle controls with the existing importer. | Real Office host duration, sync count, memory and cancellation; dates/formats/merges/errors preserved. |
| P10 — P2 | C:/work/prereasoner-data/engine/master.py:177, :246 reloads key catalogs and repeats discovery while selecting references. | Bounded key-driven retrieval or versioned catalog reuse in the existing reference owner; address N07 before optimizing prefixes. | References/keys scanned, DB transfer and FK time; exact equality, tenant isolation and multi-hop recall. |
| P11 — P2 | C:/work/prereasoner-data/engine/master.py:207, :273 copies and case-folds all cells of working/unjoined tables, then runs another discovery pass for warnings. | Restrict warning-only normalization to candidate key columns and reuse request-local evidence within the same discovery owner. | Cells copied, peak memory and FK time for many unrelated references and large uploads; exact selection and warning behavior unchanged. |

### Performance implications worth separating

**Candidate execution:** max_candidates defaults to 25. Executing candidates that the mandatory-contract check will later reject adds work without increasing eligible recall. Filtering first must use proven necessary constraints; speculative ranking/pruning is a different change and can lose correct candidates.

**Timeout accounting:** with a live request budget, each ordinary _TimedCursor.execute currently adds a configuration query. Reporting only the following SQL can understate database work substantially. Fix the measurement before claiming a DB optimization.

**Replay contention:** a 60-second wait can create roughly 240 polling cycles and at least 480 explicit INSERT/SELECT statements per waiting caller, before timeout-configuration statements. This is an idealized count from the loop, not a production measurement. The server reaches replay claiming before the ordinary WORLD_RATE check, so that limit does not bound all duplicate-wait polling.

**Trace versus parity:** SQL intermediate materialization is sometimes necessary to verify both emitters. A 50-row display cap must not silently become a 50-row correctness check. Separate trace previews from full parity requirements inside the existing runtime contract.

**Existing optimizations should be retained:** the per-schema cell-word cache, request-local FK memo, distinct-value reduction, source-table reuse, and chat prose buffer are already present. Do not add duplicate caches or propose a second prose-buffer implementation. P06 and P11 concern costs that remain around those optimizations.

**Excel lifecycle:** the upload reader already has worker timeout/termination behavior, while normalizeInWorker does not. This is a bounded-lifecycle opportunity; no live Office hang was demonstrated.

## 7. Accuracy evidence and the next improvement sequence

### Recorded Spider results

C:/work/prereasoner-data/spider/results/RESULTS.md records the release-review fixes with the same standard contract as the preceding run: whole_db, served selection, SQL backend, cap 5,000, Gemini disabled.

| Metric | Recorded count | Percentage |
|---|---:|---:|
| Examples | 1,034 | 100% |
| Strict correct | 247 | 23.9% of all examples |
| Lenient correct | 315 | 30.5% of all examples |
| Answered | 414 | 40.0% of all examples |
| Unanswered | 620 | 60.0% of all examples |
| Strict incorrect among answered | 167 | 40.3% of answered |
| Lenient incorrect among answered | 99 | 23.9% of answered |

Conditional correctness is 247/414 = 59.7% strict and 315/414 = 76.1% lenient. These denominators matter: “414 answered” is not “414 correct.”

The documented transition is **all 1,034 SQL statements and grades unchanged: zero wins, zero losses**. That means 247 unchanged strict-correct examples and 787 unchanged examples not strict-correct, including unanswered cases. No measured Spider accuracy gain should be attributed to these fixes.

The evaluator passes declared Spider foreign keys. It therefore does not exercise the ordinary inference scenario in N01 in the same way. Geographic gating and the workbook importer are not on this evaluation path. An unchanged score cannot validate N02, N03, B15 or real private-reference selection.

The recorded latency changed from median 1.18 to 1.08 seconds, p90 2.45 to 2.39 seconds, and maximum 5.96 to 5.58 seconds. The baseline shared the desktop with hermetic suites while the subsequent run mostly did not. This is not a controlled causal performance result.

The full run predates the complete source fingerprint commit. The latest _source_paths change repairs future source identity; it does not retroactively supply every missing hash to the old run, and N05 remains for non-source inputs.

### Recommended accuracy work, in order

1. **Make source values, units and key equality trustworthy.** Address B01, N01, N02 and N07 in their existing ingestion/reference/schema owners. An accurate planner cannot repair source identity or units after information has been erased.
2. **Strengthen semantic proofs.** Address B03, B05, B06 and N04 with typed requested constraints and mandatory-predicate semantics. Keep execution eligibility separate from meaning.
3. **Bind source provenance across the whole request.** Address B08, B09 and B10. Matching emitters can agree on stale or wrong source data; source versions must be part of the proof.
4. **Fix route meaning and Unicode handling.** Address N03 and B14 using shared subject/schema evidence. Extend the current router/intent owners; do not add a second planner or question-specific phrase patches.
5. **Close evaluation identity gaps before reusing results.** Address N05, B18 and B21; then run fresh serving-faithful evaluations with complete source/model/database identity.
6. **Classify the remaining benchmark failures before training.** Measure routing, candidate-pool recall, ranking, execution and coverage separately. Improve a measured failure family in the existing owner. Training would require a separately authorized experiment and unchanged baseline contract.

Useful additional families include supported equivalent numeric wording, negation coordination and transformed operands, exact punctuation in literals, multilingual schema mentions, imported display/storage units, and incomplete join coverage. Failures in these families should yield a clear bounded clarification when the contract cannot prove an answer.

### Regression matrix for corrective work

| Family | Positive control | Adversarial/negative control | Required proof |
|---|---|---|---|
| Identifier ingestion | Numeric measures still aggregate | 001 versus 1; literal quote pairs | Raw identity and intended storage preserved |
| Join equality | Ordinary exact text/numeric joins | Mixed alphanumeric columns; case/zero/scientific differences | Inference, grounding and actual row retention agree |
| Units | Explicit 20 percent / 0.2 fraction | 0.2 stored with percentage display; unknown/mixed format | Correct interpreted unit and computed amount |
| Polarity/equality | Correct ordinary exclusions and equality | Wrapped wrong exclusion; wrong field/value; OR branch | Requested mandatory predicate proved |
| Ranking/substring | Normal top/bottom and containment | Wrong direction; literal %/_/backslash | Requested order and literal identity retained |
| Geographic routing | Cities near a known city | Unicode own-data subject; unsupported category/radius/count | Correct source and typed subject/modifiers |
| Reference cap | Small complete relevant lookup | Prefix plus tail with high-multiplicity missing key | Complete total or explicit bounded omission |
| Persistence/concurrency | Current snapshot round-trip | Late old save; second upload between planning/execution | Answer-source version remains exact |
| Temporal FX | Weekend carry within policy | Rebuild old feed; withdrawn/missing series | Validity derives from real observation |
| Numeric parity | Exact ordinary arithmetic | Halfway/negative division and AVG | One rounding policy in every backend |
| Evaluation/release | Identical contract resumes/passes | Changed threshold/DB/full source/model/hosted evidence | Full input and deployed identities verified |

After focused regressions, run the relevant local suites, actual PostgreSQL tests, real add-on tests for host-dependent changes, the complete deployed Chrome prompt/follow-up gate, and a fresh whole_db served evaluation. Report strict/lenient counts, answered denominator, wins/losses and controlled latency. Do not claim an accuracy gain for unchanged benchmark answers.

## 8. Completion and limitations

Only this report was added. No production source, model bundle, deployment, repository rules, or add-on UI was changed; the earlier report was preserved. No privacy text was proposed for the Google Sheets sidebar or Excel task pane.

The local web/browser and selected static checks pass. The local Python release gate remains red because of the earlier report's 69 incompatible hyperlinks. More substantively, the current code still contains the reproduced and statically established issues above. Resolving a small set of original examples was useful progress, but does not establish that the wider failure families are fixed.
