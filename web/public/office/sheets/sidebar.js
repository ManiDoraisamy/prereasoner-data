(function () {
        var REASON_BASE = window.PrereasonerShell.reasonBase;
        var PRIVACY_URL = 'https://chat.prereasoner.com/privacy';
        var R = window.PrereasonerTurnRenderer;
        var threadEl = document.getElementById('thread');
        var scrollEl = document.getElementById('scroll');
        var noteEl = document.getElementById('note');
        var syncStatusEl = document.getElementById('syncStatus');
        var questionEl = document.getElementById('question');
        var sendEl = document.getElementById('send');
        var newEl = document.getElementById('newConversation');
        var suggestions = window.PrereasonerSuggestions.create({
          container: document.getElementById('suggestions'), composer: questionEl,
          request: function (schema) { return callServer('getPrereasonerSuggestions', schema); }
        });
        var workbookSchema = null;
        var metadataGeneration = 0;
        var lastSyncedAt = 0;
        var relativeSyncTimer = null;
        function refreshSuggestions() {
          var schema = window.PrereasonerSuggestions.merge(workbookSchema, state.tables);
          if (schema) suggestions.update(schema);
        }
        function readSuggestionMetadata() {
          var generation = ++metadataGeneration;
          callServer('getWorkbookSchema', {scope: state.scope}).then(function (schema) {
            if (generation !== metadataGeneration) return;
            workbookSchema = schema;
            refreshSuggestions();
          }).catch(function () {});
        }
        var state = {conversationId: null, turns: [], history: [], tables: [], syncedFingerprint: '', ready: false,
          restored: false, sheetError: '', uid: null, signedIn: null, busy: true, live: null, failed: null,
          importNote: '', note: '', scope: 'auto'};

        function callServer(name, arg) {
          return new Promise(function (resolve, reject) {
            var runner = google.script.run.withSuccessHandler(resolve).withFailureHandler(reject);
            if (arg === undefined) runner[name]();
            else runner[name](arg);
          });
        }

        // In a browser signed in to several Google accounts, Apps Script sends the sidebar's calls as the
        // browser's default account, not the account that opened the sheet. Google then refuses them with
        // one of these messages (all seen in the add-on's logs); none of them is about the sheet.
        var ACCOUNT_MISMATCH = /PERMISSION_DENIED|Authorization is required to perform that action|You do not have permission to access the requested document|No item with the given ID could be found/i;
        var ACCOUNT_MESSAGE = 'Google could not run Prereasoner for the account that opened this sheet. This happens when ' +
          'the browser is signed in to more than one Google account. Open the sheet in a window signed in only to the ' +
          'account that installed Prereasoner (an Incognito window works), then choose Extensions → Prereasoner → Ask a question.';

        function errorText(error) {
          var message = String((error && error.message) || error || 'Something went wrong.');
          return ACCOUNT_MISMATCH.test(message) ? ACCOUNT_MESSAGE : message;
        }

        // Sign-in for the live trace: the shared Firebase module arrives as a module script.
        var liveModule = new Promise(function (resolve) {
          if (window.__prereasonerLive) resolve(window.__prereasonerLive);
          else window.addEventListener('prereasoner-live', function () { resolve(window.__prereasonerLive); }, {once: true});
        });
        function signIn(token) {
          var timeout = new Promise(function (_, reject) {
            window.setTimeout(function () { reject(new Error('Live updates are unavailable.')); }, 15000);
          });
          return Promise.race([liveModule, timeout]).then(function (live) { return live.signIn(token); })
            .then(function (uid) { state.uid = uid; });
        }

        // The sheet through the web upload importer: the same header, date, duration, merge, total and
        // formula-error rule as an upload of the same cells.
        function importGrids(workbook) {
          if (workbook.skipped && workbook.skipped.length) {
            throw new Error('These visible tabs could not be included: ' + workbook.skipped.map(function (tab) {
              return typeof tab === 'string' ? tab : tab.name;
            }).join(', ') + '. Hide or close unrelated large tabs, then try again.');
          }
          state.scope = workbook.scope || (state.scope === 'auto' ? 'all' : state.scope);
          var result = window.WORKBOOK_IMPORT.convert({grids: workbook.grids}, window.XLSX, window.UPLOAD_LIMITS);
          if (!result.ok) throw new Error(result.error);
          if (!result.sheets.length) throw new Error('There are no data rows in the selected sheets. Choose a populated tab.');
          state.importNote = result.sheets.map(function (sheet) {
            var warnings = sheet.import && sheet.import.warnings || [];
            return warnings.length ? 'Sheet "' + sheet.name + '": ' + warnings.join(' ') : '';
          }).filter(Boolean).join(' ');
          if (state.scope === 'active') state.importNote = 'Using the current sheet because the whole workbook is too large to analyze at once. ' + state.importNote;
          return result.sheets.map(function (sheet) {
            return {name: sheet.name, data: sheet.csv, source: {kind: 'google-sheets-addon',
              warnings: (sheet.import && sheet.import.warnings || []).map(function (warning) { return warning.slice(0,4096); }),
              scope: {mode: state.scope, included: result.sheets.map(function (s) { return s.name; }),
                available: workbook.availableTabs || result.sheets.map(function (s) { return s.name; })}}};
          });
        }

        async function fingerprint(tables) {
          var text = JSON.stringify(tables.map(function (table) { return [table.name, table.data]; }));
          var hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
          return Array.prototype.map.call(new Uint8Array(hash), function (byte) {
            return byte.toString(16).padStart(2, '0');
          }).join('');
        }

        function sourceName() {
          return state.tables.length === 1 ? state.tables[0].name : 'your data';
        }

        function randomId() {
          var bytes = new Uint8Array(16);
          crypto.getRandomValues(bytes);
          return Array.prototype.map.call(bytes, function (byte) { return byte.toString(16).padStart(2, '0'); }).join('');
        }

        function analysisUrl(turn) {
          if (!turn.conversationId) return '';
          var url = REASON_BASE + encodeURIComponent(turn.conversationId);
          var analysis = turn.analysis, id = analysis && String(analysis.analysis_id || '');
          var revision = analysis && Number(analysis.revision);
          if (/^a_[0-9a-f]{32}$/.test(id) && Number.isInteger(revision) && revision >= 1 && revision <= 1000000) {
            url += '?analysis_id=' + encodeURIComponent(id) + '&revision=' + encodeURIComponent(revision);
          }
          return url;
        }

        // The web rail's reasoning panel (lib/turn-renderer.js): the same steps, sentences and backend badges.
        function reasoningHtml(turn, live) {
          var steps = turn.steps || [], asks = turn.asks || [];
          if (!steps.length && !asks.length) return '';
          var url = live ? '' : analysisUrl(turn);
          var tree = R.renderReasoningTree(steps, {renderStep: function (step, index) {
            return R.renderStepLink(step, index, url ? {href: url, title: 'Open this reasoning in Prereasoner'} : {});
          }});
          var name = turn.analysis ? R.analysisName(turn.analysis) : '';
          return R.renderReasoningPanel({
            title: name ? 'Reasoning steps for ' + name : 'Reasoning steps',
            bodyHtml: R.renderAsks(asks) + tree, analysisUrl: url, open: !!live, className: live ? 'live-reasoning' : ''
          });
        }

        function turnHtml(turn) {
          return R.renderTurn({question: turn.question, assistantHtml: R.renderAssistantTurn({
            reasoningHtml: reasoningHtml(turn, false), reply: turn.reply || 'No answer was returned.'})});
        }

        function liveHtml(live) {
          return R.renderTurn({question: live.question, assistantHtml: '<div class="turn-content">' +
            '<div class="statusline"><span class="spin"></span>' + R.escapeHtml(live.status) + '</div>' +
            reasoningHtml(live, true) +
            (live.reply ? '<div class="turn-answer convmsg answer">' + R.renderMarkdown(live.reply) + '</div>' : '') +
            '</div>'});
        }

        function failedHtml(failed) {
          return R.renderTurn({question: failed.question,
            assistantHtml: '<div class="turn-content"><div class="answer error" role="alert">' + R.escapeHtml(failed.error) + '</div></div>'});
        }

        function emptyHtml() {
          if (state.sheetError) {
            return '<div class="empty sheet-error">' + R.escapeHtml(state.sheetError) + '</div>';
          }
          return '<div class="empty"><div>Ask a question about the current sheet.</div><p class="data-notice">When you send a question, ' +
            'Prereasoner securely processes your question and the sheets selected above to produce and save ' +
            'the answer. This data is not used to train generalized AI models. <a href="' + PRIVACY_URL +
            '" target="_blank" rel="noopener">Privacy</a></p></div>';
        }

        function scrollToEnd() {
          window.requestAnimationFrame(function () { scrollEl.scrollTop = scrollEl.scrollHeight; });
        }

        function render() {
          var html = state.turns.map(turnHtml).join('');
          if (state.failed) html += failedHtml(state.failed);
          if (state.live) html += '<div id="liveTurn">' + liveHtml(state.live) + '</div>';
          if (!html) html = emptyHtml();
          threadEl.innerHTML = html;
          scrollToEnd();
        }

        // A live event repaints only the running turn, so earlier turns keep their open panels.
        function renderLive() {
          var element = document.getElementById('liveTurn');
          if (!element || !state.live) return render();
          element.innerHTML = liveHtml(state.live);
          scrollToEnd();
        }

        // Describe import warnings and source scope alongside recovery messages.
        function showNote(text) {
          state.note = text || '';
          var combined = [state.importNote, state.note].filter(Boolean).join(' ');
          noteEl.textContent = combined;
          noteEl.hidden = !combined;
        }

        function relativeSyncText(ageMs) {
          var minutes = Math.floor(ageMs / 60000);
          if (minutes < 1) return 'Synced just now';
          if (minutes < 60) return 'Updated ' + minutes + (minutes === 1 ? ' minute' : ' minutes') + ' ago';
          var hours = Math.floor(minutes / 60);
          if (hours < 24) return 'Updated ' + hours + (hours === 1 ? ' hour' : ' hours') + ' ago';
          var days = Math.floor(hours / 24);
          return 'Updated ' + days + (days === 1 ? ' day' : ' days') + ' ago';
        }

        function setSyncStatus(status, stateName) {
          syncStatusEl.textContent = status;
          syncStatusEl.dataset.state = stateName || 'synced';
        }

        function markSynced() {
          lastSyncedAt = Date.now();
          setSyncStatus(relativeSyncText(0), 'synced');
          if (relativeSyncTimer === null) relativeSyncTimer = window.setInterval(function () {
            if (lastSyncedAt) setSyncStatus(relativeSyncText(Date.now() - lastSyncedAt), 'synced');
          }, 30000);
        }

        function setBusy(busy) {
          state.busy = busy;
          // Reading and answering must never lock the user's draft.
          questionEl.disabled = false;
          sendEl.disabled = busy;
          newEl.disabled = busy || !state.ready;
          sendEl.textContent = busy ? '…' : '↑';
        }

        // Follow the turn as it runs: the calls the assistant makes, each call's steps as they finish,
        // and the reply as it is written (lib/firebase-init.js subscribeTurn/subscribeRun).
        function startLive(live) {
          if (!state.uid || !window.subscribeTurn || !window.subscribeRun) return function () {};
          var jobs = {}, subscriptions = [];
          function refreshSteps() {
            var views = [];
            Object.keys(jobs).forEach(function (jobId) {
              var job = jobs[jobId];
              Object.keys(job.views).sort(function (a, b) { return Number(a) - Number(b); }).forEach(function (key) {
                views.push(Object.assign({}, job.views[key], {execution: job.views[key].execution || job.execution}));
              });
            });
            live.steps = R.stepsFromViews(views, {sourceName: sourceName()});
            renderLive();
          }
          subscriptions.push(window.subscribeTurn(state.uid, live.turnId, {
            onStatus: function (status) { if (status === 'done') live.done = true; },
            onCall: function (_, call) {
              if (!call || !call.jobId || jobs[call.jobId]) return;
              jobs[call.jobId] = {views: {}, execution: null};
              live.asks.push(String(call.question || ''));
              live.status = 'Reading as: “' + call.question + '”…';
              renderLive();
              subscriptions.push(window.subscribeRun(state.uid, call.jobId, {
                onView: function (key, view) {
                  if (!view || jobs[call.jobId].views[key]) return;
                  jobs[call.jobId].views[key] = view;
                  live.status = R.stepStatus(view);
                  refreshSteps();
                },
                onExecution: function (execution) { jobs[call.jobId].execution = execution; refreshSteps(); },
                onAnalysis: function (analysis) { if (analysis) { live.analysis = analysis; renderLive(); } },
                onTransportError: function (message) { live.status = message; renderLive(); }
              }));
            },
            onReply: function (text) { live.reply = String(text || ''); renderLive(); },
            onConversation: function (id) { if (id) live.conversationId = String(id); }
          }));
          return function () {
            subscriptions.forEach(function (stop) { try { stop(); } catch (_) {} });
          };
        }

        function finishedTurn(question, response) {
          var views = [], asks = [], analysis = null;
          (response.traces || []).forEach(function (trace) {
            if (trace.question) asks.push(trace.question);
            if (trace.analysis) analysis = trace.analysis;
            (trace.views || []).forEach(function (view) {
              views.push(Object.assign({}, view, {execution: view.execution || trace.execution}));
            });
          });
          return {question: question, reply: response.reply || 'No answer was returned.', asks: asks, analysis: analysis,
            steps: R.stepsFromViews(views, {sourceName: sourceName()}), conversationId: state.conversationId};
        }

        function snapshot() {
          return {client: 'google-sheets-addon', version: 2, turns: state.turns.slice(-24),
            history: state.history.slice(-24), syncedFingerprint: state.syncedFingerprint};
        }

        // Saved sidebars: version 2 keeps the shared rail's steps; version 1 (before the shared rail)
        // kept its own step labels, shown as the step sentences.
        function restoredTurns(saved) {
          if (!saved || saved.client !== 'google-sheets-addon' || !Array.isArray(saved.turns)) return [];
          return saved.turns.slice(-24).map(function (turn) {
            if (saved.version === 2) return Object.assign({}, turn, {conversationId: state.conversationId});
            return {question: turn.question, reply: turn.reply, analysis: turn.analysis || null, asks: [],
              conversationId: state.conversationId, steps: (turn.reasoning || []).map(function (step, index) {
                return Object.assign({}, step, {index: index, description: step.label || ''});
              })};
          });
        }

        async function restore(tables) {
          try {
            var restored = await callServer('restorePrereasonerSheetConversation', {tables: tables});
            state.conversationId = restored.conversationId || null;
            state.turns = restoredTurns(restored.state);
            state.history = restored.state && Array.isArray(restored.state.history) ? restored.state.history.slice(-24) : [];
            state.syncedFingerprint = (restored.state && restored.state.syncedFingerprint) || '';
            state.restored = true;
            if (restored.stale && state.turns.length) showNote('The sheet changed since this conversation. Ask again to use the current data.');
          } catch (_) {
            // Restoring old sidebar history is best effort; it must not gate a question over the
            // freshly read workbook. A dangling/expired session starts a new chat automatically.
            state.conversationId = null;
            state.turns = [];
            state.history = [];
            state.syncedFingerprint = '';
            state.restored = true;
            showNote('Your previous chat could not be resumed. This question will start a new chat.');
          }
        }

        async function submit() {
          var question = questionEl.value.trim();
          if (!question || state.busy) return;
          questionEl.value = '';
          state.failed = null;
          showNote('');
          setBusy(true);
          var live = {question: question, turnId: randomId(), status: 'Reading the sheet…', steps: [], asks: [], reply: ''};
          setSyncStatus('Syncing…', 'loading');
          state.live = live;
          render();
          var stopLive = function () {};
          try {
            var tables = importGrids(await callServer('getWorkbookGrids', {scope: state.scope}));
            markSynced();
            showNote(state.note);
            state.sheetError = '';
            state.tables = tables;
            refreshSuggestions();
            var print = await fingerprint(tables);
            if (!state.restored) await restore(tables);
            if (state.conversationId && state.syncedFingerprint && print !== state.syncedFingerprint) {
              live.status = 'Syncing the changed sheet…';
              renderLive();
              try {
                await callServer('syncPrereasonerConversation', {conversationId: state.conversationId, tables: tables});
              } catch (_) {
                state.conversationId = null;
                state.turns = [];
                state.history = [];
                state.syncedFingerprint = '';
                showNote('Your previous chat could not be continued. This question will start a new chat with the current sheet.');
              }
            }
            live.status = 'Understanding your question…';
            renderLive();
            if (state.signedIn) await Promise.race([state.signedIn.catch(function () {}),
              new Promise(function (resolve) { window.setTimeout(resolve, 5000); })]);
            stopLive = startLive(live);
            var response;
            try {
              response = await callServer('askPrereasoner', {question: question, tables: tables,
                conversationId: state.conversationId, history: state.history, turnId: live.turnId});
            } catch (error) {
              if (!(live.done && live.reply)) throw error;   // the live trace already finished this turn
              response = {reply: live.reply, conversationId: live.conversationId, history: [], traces: null};
            }
            response = window.RESULT_WIRE ? window.RESULT_WIRE.decode(response) : response;
            state.conversationId = response.conversationId || live.conversationId || state.conversationId;
            var turn = response.traces ? finishedTurn(question, response)
              : {question: question, reply: live.reply, asks: live.asks, analysis: live.analysis || null, steps: live.steps,
                 conversationId: state.conversationId};
            state.turns = state.turns.concat([turn]).slice(-24);
            state.history = response.history && response.history.length ? response.history.slice(-24)
              : state.history.concat([{role: 'user', content: question}, {role: 'assistant', content: turn.reply}]).slice(-24);
            state.syncedFingerprint = print;
            state.live = null;
            render();
            callServer('savePrereasonerSheetConversation', {conversationId: state.conversationId, state: snapshot()})
              .catch(function (error) {
                showNote('The answer was returned, but this sheet’s conversation could not be saved: ' + errorText(error));
              });
          } catch (error) {
            setSyncStatus('Sync failed', 'error');
            state.live = null;
            state.failed = {question: question, error: errorText(error)};
            if (!questionEl.value.trim()) questionEl.value = question;
            render();
          } finally {
            stopLive();
            setBusy(false);
            questionEl.focus();
          }
        }

        document.getElementById('composer').addEventListener('submit', function (event) {
          event.preventDefault();
          submit();
        });
        questionEl.addEventListener('keydown', function (event) {
          if (!state.busy && event.key === 'Enter' && !event.altKey && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
            event.preventDefault();
            submit();
          }
        });
        newEl.addEventListener('click', async function () {
          if (state.busy) return;
          setBusy(true);
          try {
            await callServer('clearPrereasonerSheetConversation');
            state.conversationId = null;
            state.turns = [];
            state.history = [];
            state.syncedFingerprint = '';
            state.failed = null;
            showNote('');
            render();
          } catch (error) {
            showNote(errorText(error));
          } finally {
            setBusy(false);
          }
        });

        callServer('getSidebarContext').then(async function (context) {
          state.signedIn = signIn(context.token);
          state.signedIn.catch(function () {});
          try {
            state.tables = importGrids(context.workbook);
            markSynced();
            showNote(state.note);
          } catch (error) {
            state.sheetError = errorText(error);
            return;
          }
          // Independent of restore/answers. A slow or unavailable suggestion never blocks typing.
          refreshSuggestions();
          try {
            await restore(state.tables);
          } catch (error) {
            showNote(errorText(error));
          }
        }).catch(function (error) {
          state.sheetError = errorText(error);
          setSyncStatus('Couldn’t sync', 'error');
        }).then(function () {
          if (!state.tables.length) setSyncStatus('Couldn’t sync', 'error');
          render();
          state.ready = true;
          setBusy(false);
          questionEl.focus();
        });
        // Headers only: starts in parallel with the first full sheet read.
        readSuggestionMetadata();
      })();
