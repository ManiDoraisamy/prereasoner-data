import assert from 'node:assert/strict';
import {streamResponse} from '../public/office/excel/turn-bridge.js';

const result = streamResponse({
  status: 'done',
  reply: 'France total: $200.',
  conversation_id: 'c_0123456789abcdef0123456789abcdef',
  calls: {
    1: {jobId: 'turn_1', question: 'Calculate the total by country'},
    0: {jobId: 'turn_0', question: 'Inspect the workbook'}
  }
}, {
  turn_0: {status: 'done', views: {'0': {title: 'Source rows'}, '1': {title: 'Grouped rows'}}},
  turn_1: {status: 'done', result: {views: [{title: 'Total'}]}}
});

assert.equal(result.reply, 'France total: $200.');
assert.equal(result.conversation_id, 'c_0123456789abcdef0123456789abcdef');
assert.deepEqual(result.traces.map(trace => trace.jobId), ['turn_0', 'turn_1']);
assert.deepEqual(result.traces[0].engine.views.map(view => view.title), ['Source rows', 'Grouped rows']);
assert.deepEqual(result.traces[1].engine.views.map(view => view.title), ['Total']);
console.log('Excel streamed turn recovery: 5 checks passed');
