// Shared turn presentation for the web workspace and Google Sheets add-on.
// Both surfaces supply their own CSS and actions; this file owns the complete turn order,
// safe inline Markdown, the reasoning disclosure, and the section-aware reasoning tree.
(function (root) {
  'use strict';

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, '&#96;');
  }

  function safeHttpUrl(value) {
    var text = String(value || '').trim();
    if (!/^https?:\/\//i.test(text)) return '';
    try {
      var parsed = new URL(text);
      return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : '';
    } catch (_) {
      return '';
    }
  }

  function renderMarkdown(value) {
    var source = String(value == null ? '' : value);
    var tokens = [];
    function token(html) {
      var index = tokens.push(html) - 1;
      return '\u0000PR' + index + '\u0000';
    }

    source = source.replace(/`([^`\n]+)`/g, function (_, code) {
      return token('<code>' + escapeHtml(code) + '</code>');
    });
    source = source.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/gi, function (_, label, href) {
      var safe = safeHttpUrl(href);
      if (!safe) return escapeHtml(label);
      return token('<a href="' + escapeAttribute(safe) + '" target="_blank" rel="noopener noreferrer">' +
        escapeHtml(label) + '</a>');
    });

    var html = escapeHtml(source);
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    html = html.replace(/\n/g, '<br>');
    html = html.replace(/\u0000PR(\d+)\u0000/g, function (_, index) {
      return tokens[Number(index)] || '';
    });
    return html;
  }

  function analysisName(value) {
    return String((value && (value.display_name || value.slug)) || '')
      .replace(/_/g, ' ')
      .trim();
  }

  function normalizeSteps(steps) {
    return (Array.isArray(steps) ? steps : []).filter(function (step) {
      return step && typeof step === 'object';
    }).map(function (step, index) {
      var out = Object.assign({}, step);
      out.index = Number.isInteger(step.index) ? step.index : index;
      out.sectionId = String(step.sectionId || step.section || '');
      out.sectionLabel = String(step.sectionLabel || step.section_label || out.sectionId || '');
      out.sectionQuestion = String(step.sectionQuestion || step.section_question || '');
      out.sectionInputs = Array.isArray(step.sectionInputs || step.section_inputs)
        ? (step.sectionInputs || step.section_inputs).map(String) : [];
      return out;
    });
  }

  function defaultStepHtml(step, index) {
    var label = String(step.label || ('Step ' + (index + 1)));
    var detail = String(step.detail || '');
    return '<div class="steplink"><span class="idx">' + (index + 1) + '</span>' +
      '<span class="stx"><b>' + escapeHtml(label) + '</b>' +
      (detail ? '<span class="stepdetail">' + escapeHtml(detail) + '</span>' : '') +
      '</span></div>';
  }

  function renderReasoningTree(rawSteps, options) {
    var steps = normalizeSteps(rawSteps);
    if (!steps.length) return '';
    options = options || {};
    var renderStep = typeof options.renderStep === 'function' ? options.renderStep : defaultStepHtml;
    var bySection = new Map();
    var order = [];
    var loose = [];

    steps.forEach(function (step) {
      if (!step.sectionId) {
        loose.push(step);
        return;
      }
      if (!bySection.has(step.sectionId)) {
        bySection.set(step.sectionId, {
          id: step.sectionId,
          label: step.sectionLabel || step.sectionId,
          question: step.sectionQuestion || '',
          inputs: step.sectionInputs || [],
          steps: []
        });
        order.push(step.sectionId);
      }
      var section = bySection.get(step.sectionId);
      if (!section.question && step.sectionQuestion) section.question = step.sectionQuestion;
      if (!section.inputs.length && step.sectionInputs && step.sectionInputs.length) {
        section.inputs = step.sectionInputs.slice();
      }
      section.steps.push(step);
    });

    function stepList(items) {
      return '<div class="branchsteps">' + items.map(function (step) {
        return renderStep(step, step.index);
      }).join('') + '</div>';
    }

    if (!bySection.size) return '<div class="reasontree">' + stepList(loose) + '</div>';

    var outputStep = steps.slice().reverse().find(function (step) {
      return step.sectionId && step.isOutput;
    });
    var outputId = outputStep ? outputStep.sectionId : order[order.length - 1];
    var seen = new Set();

    function node(id, depth) {
      var section = bySection.get(id);
      if (!section) return '';
      var safeDepth = Math.min(Math.max(depth, 0), 6);
      if (seen.has(id)) {
        return '<div class="reasonref depth' + safeDepth + '">&uarr; ' + escapeHtml(section.label) + '</div>';
      }
      seen.add(id);
      var question = section.question && section.question.toLowerCase() !== section.label.toLowerCase()
        ? '<span class="branchq">' + escapeHtml(section.question) + '</span>' : '';
      var childIds = section.inputs.slice();
      if (section.steps.some(function (step) { return step.kind === 'cross' || step.op === 'cross'; })) {
        childIds.sort(function (leftId, rightId) {
          var left = bySection.get(leftId), right = bySection.get(rightId);
          var leftIndex = left && left.steps.length ? left.steps[0].index : Number.MAX_SAFE_INTEGER;
          var rightIndex = right && right.steps.length ? right.steps[0].index : Number.MAX_SAFE_INTEGER;
          return leftIndex - rightIndex;
        });
      }
      var children = childIds.map(function (source) { return node(source, depth + 1); }).join('');
      return '<div class="reasonnode depth' + safeDepth + '">' +
        '<div class="branchhead"><span class="branchmark"></span><span class="branchtxt"><b>' +
        escapeHtml(section.label) + '</b>' + question + '</span></div>' +
        (children ? '<div class="reasonchildren">' + children + '</div>' : '') +
        stepList(section.steps) + '</div>';
    }

    var html = outputId ? node(outputId, 0) : '';
    order.forEach(function (id) { if (!seen.has(id)) html += node(id, 0); });
    if (loose.length) html += stepList(loose);
    return '<div class="reasontree">' + html + '</div>';
  }

  // The rail's step presentation for the engine's views, shared by the web workspace and the Google
  // Sheets add-on: a short name, a plain-English sentence, the live "working" line, the tables a step
  // was built from, and the backend that produced its rows.
  var STEP_NAMES = {join: 'combined', world_join: 'reference lookup', world_filter: 'filtered',
    filter: 'filtered', cross: 'candidate pairs', anti_join: 'not yet matched', time_filter: 'date filter',
    having: 'filtered', group_agg: 'total', yoy: 'year-over-year', running: 'running total',
    divide: 'ratio', share: 'share', topn: 'top results', sort: 'sorted'};

  function stepLabel(view) {
    var op = view && view.op;
    if (op === 'group_agg') {
      var text = String((view.sql || '') + ' ' + (view.label || '')).toLowerCase();
      if (/\bcount\b/.test(text)) return 'count';
      if (/\bavg\b|average/.test(text)) return 'average';
      if (/\bmin\b|\bmax\b/.test(text)) return 'extremes';
      return 'total';
    }
    return STEP_NAMES[op] || (view && view.label) || op || '';
  }

  function humanCondition(condition) {
    return String(condition || '').replace(/\s*<>\s*/, ' is not ').replace(/\s*=\s*/, ' is ')
      .replace(/'/g, '').trim();
  }

  function stepDescription(view) {
    var label = (view && view.label) || '', op = view && view.op, match;
    if (op === 'join') {
      var tables = label.replace(/^join\s+/i, '').replace(/\s*\+\s*/g, ' and ');
      return 'Combined ' + (tables || 'your tables') + ' into one table.';
    }
    if (op === 'world_join') {
      match = label.match(/on\s+(.+)$/i);
      return 'Looked up shared facts for each ' + (match ? match[1].trim() : 'entity') +
        ' from the source named in the answer provenance.';
    }
    if (op === 'world_filter' || op === 'filter' || op === 'having') {
      match = label.match(/where\s+(.+)$/i);
      return match ? 'Kept only the rows where ' + humanCondition(match[1]) + '.' : 'Filtered to the matching rows.';
    }
    if (op === 'time_filter') return 'Kept only the rows in that time period.';
    if (op === 'convert') {
      return 'Converted each amount at its ECB reference rate — the rate and its publication date are ' +
        'columns on this sheet, so the Result is just the converted column summed.';
    }
    if (op === 'group_agg') {
      return {count: 'Counted the rows.', average: 'Averaged the values.',
        extremes: 'Found the highest and lowest values.'}[stepLabel(view)] || 'Added up the values to get the total.';
    }
    var sentences = {topn: 'Kept just the top-ranked results.',
      cross: 'Built every candidate pair from the two ranked branches.',
      anti_join: 'Removed pairs already present in the evidence branch.', sort: 'Sorted the results in order.',
      yoy: 'Computed the year-over-year change.', running: 'Computed a running (cumulative) total.',
      share: 'Computed each row’s share of the total.', divide: 'Computed the ratio between the two measures.'};
    return sentences[op] || label || stepLabel(view);
  }

  function stepStatus(view) {
    switch (view && view.op) {
      case 'join': return 'Combining your tables…';
      case 'world_join': return 'Looking up world facts…';
      case 'world_filter': case 'filter': case 'having': return 'Filtering the rows…';
      case 'group_agg': return 'Crunching the numbers…';
      case 'order': case 'sort': return 'Sorting the results…';
      case 'divide': return 'Working out the ratio…';
      default: return 'Working it out…';
    }
  }

  // Which backend produced a step's rows: both emitters always exist, only `ran` is evidence.
  function executionRan(execution) {
    var actual = execution && execution.actual ? String(execution.actual) : '';
    if (actual === 'verify' && execution.verified) return 'both';
    return actual === 'python' ? 'py' : actual === 'sql' ? 'sql' : '';
  }

  function renderExecutionBadge(ran) {
    var text = ran === 'both' ? 'PY = SQL' : ran === 'py' ? 'PY ran' : ran === 'sql' ? 'SQL ran' : '';
    return text ? '<span class="stepbackend ' + ran + '" title="Execution backend for this materialized step">' +
      text + '</span>' : '';
  }

  // The tables a step was built from. `step` and `steps` carry {viewName, name, sectionId, sectionLabel,
  // inputs, sql}; `sourceName` names the uploaded data when a canonical table id appears.
  function stepLineage(step, steps, sourceName) {
    function clean(value) {
      return String(value || '').replace(/\bc_[0-9a-f]{32}\b/gi, sourceName || 'your data');
    }
    var inputs = Array.isArray(step.inputs) ? step.inputs : [];
    if (inputs.length) {
      return inputs.map(function (value) {
        var source = (steps || []).filter(function (item) { return item.viewName === value; })[0];
        if (source) {
          return source.sectionId && source.sectionId !== step.sectionId && source.sectionLabel
            ? source.sectionLabel : source.name;
        }
        return clean(value).replace(/_/g, ' ');
      }).join(', ');
    }
    var names = [], pattern = /(?:from|join)\s+"([^"]+)"/gi, match;
    while ((match = pattern.exec(String(step.sql || '')))) {
      if (!/world|meaning/i.test(match[1]) && names.indexOf(match[1]) < 0) names.push(match[1]);
    }
    return clean(names.slice(0, 4).join(', '));
  }

  // One numbered step: its sentence, its lineage, and its backend. The web opens the step's sheet
  // (`onclick`); the add-on opens the full analysis (`href`); a step still streaming opens nothing.
  function renderStepLink(step, index, options) {
    options = options || {};
    var description = String(step.description || step.name || '').replace(/^(\w+)\s+\1\b/i, '$1');
    var lineage = String(step.lineage || '');
    var inner = '<span class=idx>' + (index + 1) + '</span><span class=stx>' + escapeHtml(description) +
      (lineage ? '<span class=steplin> · from ' + escapeHtml(lineage) + '</span>' : '') + '</span>' +
      renderExecutionBadge(step.ran || '');
    var className = 'steplink' + (options.active ? ' on' : '');
    var title = options.title ? ' title="' + escapeAttribute(options.title) + '"' : '';
    if (options.href) {
      return '<a class="' + className + '" href="' + escapeAttribute(options.href) +
        '" target="_blank" rel="noopener noreferrer"' + title + '>' + inner + '</a>';
    }
    if (!options.onclick) return '<div class="' + className + '"' + title + '>' + inner + '</div>';
    return '<button class="' + className + '"' + title + ' onclick="' + escapeAttribute(options.onclick) + '">' +
      inner + '</button>';
  }

  // The questions the assistant asked the engine this turn ("read as …").
  function renderAsks(questions) {
    var list = (questions || []).filter(Boolean);
    return list.length ? '<div class=cotask>read as ' + list.map(function (question) {
      return '&ldquo;' + escapeHtml(question) + '&rdquo;';
    }).join(', ') + '</div>' : '';
  }

  // Engine views (live or from a /chat trace) as reasoning-tree steps, in the web rail's presentation.
  function stepsFromViews(views, options) {
    options = options || {};
    var steps = (views || []).filter(function (view) { return view && typeof view === 'object'; })
      .map(function (view, index) {
        return {index: index, viewName: String(view.name || ''), name: stepLabel(view),
          description: stepDescription(view), kind: String(view.op || ''), inputs: view.inputs || [],
          sql: view.sql || '', ran: executionRan(view.execution || options.execution),
          isOutput: !!view.is_output, sectionId: String(view.section || ''),
          sectionLabel: String(view.section_label || ''), sectionQuestion: String(view.section_question || ''),
          sectionInputs: view.section_inputs || []};
      });
    steps.forEach(function (step) { step.lineage = stepLineage(step, steps, options.sourceName); });
    return steps;
  }

  function renderReasoningPanel(options) {
    options = options || {};
    var body = String(options.bodyHtml || '');
    if (!body && !options.title && !options.titleHtml) return '';
    var title = options.titleHtml != null ? String(options.titleHtml) : escapeHtml(options.title || 'Reasoning steps');
    var url = safeHttpUrl(options.analysisUrl);
    var className = 'turn-reasoning reasoning' + (options.className ? ' ' + escapeAttribute(options.className) : '');
    var toggle = options.onToggle ? ' ontoggle="' + escapeAttribute(options.onToggle) + '"' : '';
    return '<details class="' + className + '"' + (options.open ? ' open' : '') + toggle + '>' +
      '<summary class="reasoning-header cotbar"><span class="reasoning-toggle cotbtn" aria-hidden="true">' +
      '<span class="cotchev">&#8250;</span></span><span class="reasoning-title">' + title + '</span></summary>' +
      '<div class="reasoning-body">' +
      (url ? '<a class="analysis-open" href="' + escapeAttribute(url) +
        '" target="_blank" rel="noopener noreferrer">Open full analysis &#8599;</a>' : '') +
      body + '</div></details>';
  }

  function renderAssistantTurn(options) {
    options = options || {};
    var answer = options.answerHtml != null
      ? String(options.answerHtml)
      : renderMarkdown(options.reply || '');
    return '<div class="turn-content">' + String(options.reasoningHtml || '') +
      '<div class="turn-answer convmsg answer">' + answer + '</div>' +
      String(options.afterHtml || '') + '</div>';
  }

  function renderTurn(options) {
    options = options || {};
    return '<div class="turn-pair"><div class="turn user"><div class="msg question">' +
      escapeHtml(options.question || '') + '</div></div><div class="turn ai">' +
      String(options.assistantHtml || '') + '</div></div>';
  }

  root.PrereasonerTurnRenderer = {
    analysisName: analysisName,
    escapeAttribute: escapeAttribute,
    escapeHtml: escapeHtml,
    executionRan: executionRan,
    renderAsks: renderAsks,
    renderAssistantTurn: renderAssistantTurn,
    renderExecutionBadge: renderExecutionBadge,
    renderMarkdown: renderMarkdown,
    renderReasoningPanel: renderReasoningPanel,
    renderReasoningTree: renderReasoningTree,
    renderStepLink: renderStepLink,
    renderTurn: renderTurn,
    safeHttpUrl: safeHttpUrl,
    stepDescription: stepDescription,
    stepLabel: stepLabel,
    stepLineage: stepLineage,
    stepStatus: stepStatus,
    stepsFromViews: stepsFromViews
  };
}(typeof window === 'undefined' ? globalThis : window));
