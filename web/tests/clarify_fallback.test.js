// Dependency-free checks for the no-Sonnet clarify text in the production classic script.
// Run: node web/tests/clarify_fallback.test.js
//
// This path is only reached when /api/converse is unavailable, which is exactly when a wrong message
// is most costly: there is no LLM left to soften it. An UNMET requirement means the engine understood
// the question and the reference data cannot answer it, so the generic "I couldn't map that to a
// query" line would be plainly false.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
const match = source.match(/function clarifyFallbackText\(c\)\{[\s\S]*?\n\}/);
assert(match, 'clarifyFallbackText not found in workbook.js');

const context = {};
vm.createContext(context);
vm.runInContext(match[0] + '; this.clarifyFallbackText = clarifyFallbackText;', context);
const text = context.clarifyFallbackText;

// 1. Unmet requirement WITH an alternative the data can supply: name what is missing, what exists,
//    and offer the rephrasing. Must not claim the question was un-mappable.
const withAlternative = text({
  clarify: true, proposed: 'total order amount in US dollars',
  unmet: [{requested: 'EUR', detail: 'rate_to_eur', available: ['USD']}],
});
assert(withAlternative.includes('EUR'), withAlternative);
assert(withAlternative.includes('USD'), withAlternative);
assert(withAlternative.includes('total order amount in US dollars'), withAlternative);
assert(!/couldn.t map that/i.test(withAlternative), 'must not claim the question was un-mappable');

// 2. Unmet requirement with NO alternative (no rate column at all): still honest, still no false claim.
const noAlternative = text({
  clarify: true, proposed: '',
  unmet: [{requested: 'EUR', detail: 'rate_to_eur', available: []}],
});
assert(noAlternative.includes('EUR'), noAlternative);
assert(!/couldn.t map that/i.test(noAlternative), 'must not claim the question was un-mappable');
assert(!/only carries/.test(noAlternative), 'must not claim to carry rates when it carries none');

// 3. The two pre-existing shapes are untouched — a coverage clarify and a bare one.
const ordinary = text({clarify: true, proposed: 'total sales by country'});
assert(ordinary.startsWith('Did you mean'), ordinary);
assert(/trace panel/.test(ordinary), ordinary);
const bare = text({clarify: true});
assert(/couldn.t map that/i.test(bare), bare);

console.log('clarify fallback: 4 cases passed');
