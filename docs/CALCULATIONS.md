# Deterministic calculations

Prereasoner treats arithmetic as part of the query, not as a number added after the query runs.
Typed AST nodes represent the operands and operation. The shared encoder helps identify and order
those operands; it never generates SQL or decides whether a result is correct. A numeric result is
released only when a registered specification proves that the selected query matches the request.

## Serving flow

1. `registry.detect_calculations()` extracts an explicit calculation request.
2. The existing encoder supplies role-specific column similarities such as `numerator`,
   `denominator`, `measure`, and `rate`.
3. Each specification binds only type- and domain-eligible columns and returns `CalculationPlan`
   objects containing AST expressions, units, rules, and named bindings.
4. `CalculationQueryExpander` adds those expressions to the ordinary bounded candidate pool and
   obtains joins from `SchemaGraph`, including complete composite keys.
5. Normal deterministic ranking orders the pool. Learned similarities are named soft features.
6. `describe_computation()` records each selected expression, complete join predicate, guaranteed
   filter, and set-operation branch.
7. The registry selects the first ranked candidate satisfying every detected calculation. When none
   does, serving removes the numeric result and returns structured evidence explaining what is missing.

## Registered specifications

| Specification | Accepted shape | Required evidence |
|---|---|---|
| Currency | filter, identity/unit annotation, or `SUM(amount * rate_to_target)` | ISO target, monetary measure, exact typed rate edge in the selected AST, complete branch coverage |
| Ratio | `SUM(numerator) / SUM(denominator)` | two eligible numeric roles, selected complete registered-key path, same operands on every branch |
| Rate application | rate amount, `SUM(amount * rate)`; inclusive total, `SUM(amount * (1 + rate))`; deducted total, `SUM(amount * (1 - rate))` | monetary measure, dimensionless flat rate, unambiguous direction, selected complete registered-key path, temporal coordinate when the rate is dated |

Rate application recognizes flat tax/VAT, commission, discount, and explicit annual one-year simple
interest. A named category such as customer tier or payment instrument can select one flat rate per
row through an ordinary typed key join. Per-capita is a named ratio whose denominator must bind to a
population/person measure. Division renders with real-valued semantics and a
`NULLIF(denominator, 0)` guard.

The generic calculation planner deliberately abstains on tiered or progressive schedules,
compound or variable-period interest, latest-prior/as-of joins, missing temporal alignment, unknown
rate units, and ambiguous add/subtract direction or operand bindings. Compatible row factors compose
inside one aggregate: for example, currency conversion plus a customer-tier discount becomes one
`SUM(amount * exchange_rate * (1 - discount_rate))`. The verifier requires the final AST to equal a
composition of every detected specification; proving either factor alone is insufficient. A question
that merely asks for a tax, commission, or discount *rate* is an ordinary projection, not a request to
apply that rate.

Own-data AST search and scalar world-filter queries consume the same registered calculation plan.
The world adapter owns its request-local entity bridge, but it renders the registry's typed scalar
expression and returns that same expression as verification evidence; it does not maintain a separate
currency-only formula. Calculated world projections/groupings remain fail-closed until their grouping
columns are represented in that typed bridge.

ECB conversion does not weaken that rule. Its offline projection expands the active source release
to exact calendar-date rows and preserves both the true source business date and release ID. Serving
therefore performs a normal composite equality join and verifies the same typed currency expression;
it does not synthesize a latest-prior predicate inside the generic planner.

## Extension contract

Add one immutable specification to `engine/calculations/specifications.py` and register exactly one
instance in `SPECIFICATIONS`. It must implement deterministic `detect`, `plans`, and `assess` methods.
Plans contain AST nodes, never SQL strings. Assessment consumes `ComputationEvidence`, never rendered
SQL. A matching arithmetic expression is insufficient: every bound table must be reachable in that
branch through complete join predicates matching registered `SchemaGraph` edges. Add adversarial tests
for wrong operand types, partial composite joins, every set-operation branch, unsupported policies,
and missing typed evidence.

If operation or operand retrieval needs improvement, extend
`training/props/calculation_contrastive.py` with train and query-disjoint heldout examples. Aggregate
intent regressions belong in `training/props/augment_intent.py`; its release probes include ranking
questions where a number denotes `LIMIT`, not `COUNT`. Regenerate both corpora, train the existing
property/intent LoRA, and gate it with both `eval_intent` and `eval_calculations`. Do not add a
calculation-only model.

## Compatibility

Responses expose the canonical `calculations` list. Currency assessments are also copied to the
legacy `currency` field while clients migrate. The compatibility field is a projection of the same
assessment, not a second policy path.
