// Shared answer + reasoning presentation for the web workspace and Google Sheets add-on.
// Both surfaces supply their own CSS and actions; this file owns safe inline Markdown and
// the section-aware reasoning tree so one client cannot silently flatten the other.
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

  root.PrereasonerTurnRenderer = {
    analysisName: analysisName,
    escapeAttribute: escapeAttribute,
    escapeHtml: escapeHtml,
    renderMarkdown: renderMarkdown,
    renderReasoningTree: renderReasoningTree,
    safeHttpUrl: safeHttpUrl
  };
}(typeof window === 'undefined' ? globalThis : window));
