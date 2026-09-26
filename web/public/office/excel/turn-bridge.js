function childValues(value) {
  if (Array.isArray(value)) return value.filter(Boolean);
  if (!value || typeof value !== 'object') return [];
  return Object.keys(value)
    .sort((left, right) => Number(left) - Number(right))
    .map(key => value[key])
    .filter(Boolean);
}

export function streamResponse(turn, engineRuns) {
  const calls = childValues(turn?.calls);
  const traces = calls.filter(call => call.jobId).map(call => {
    const run = engineRuns?.[call.jobId] || {};
    const views = run.views ?? run.result?.views ?? [];
    return {
      jobId: call.jobId,
      question: call.question || '',
      engine: {...run, views: childValues(views)}
    };
  });
  return {
    reply: typeof turn?.reply === 'string' ? turn.reply : '',
    conversation_id: turn?.conversation_id || null,
    traces
  };
}
