(function () {
        var REASON_BASE = window.PrereasonerShell.reasonBase;
        var R = window.PrereasonerTurnRenderer;
        var threadEl = document.getElementById('thread');
        var scrollEl = document.getElementById('scroll');
        var toplineEl = document.getElementById('topline');
        var questionEl = document.getElementById('question');
        var sendEl = document.getElementById('send');
        var newEl = document.getElementById('newConversation');
        var syncEl = document.getElementById('syncStatus');
        // Starters show as soon as the headers are read, while the cells are still being read; a saved
        // chat that comes back replaces them. They waited for the whole read, ten minutes on a six-tab
        // workbook (2026-10-04).
        var suggestions = window.PrereasonerSuggestions.create({
          container: document.getElementById('suggestions'), composer: questionEl,
          request: function (schema) { return callServer('getPrereasonerSuggestions', schema); }
        });
        var workbookSchema = null;
        var metadataGeneration = 0;
        function refreshSuggestions() {
          var schema = window.PrereasonerSuggestions.merge(workbookSchema, state.tables);
          if (schema) suggestions.update(schema);
        }
        function readSuggestionMetadata() {
          var generation = ++metadataGeneration;
          callServer('getWorkbookSchema', {scope: state.scope}).then(function (schema) {
            if (generation !== metadataGeneration) return;
            workbookSchema = schema;
            if (state.loading && schema && Array.isArray(schema.scope) && schema.scope.length) {
              state.loading.tabs = schema.scope.length;
              if (!state.turns.length && !state.live) render();
            }
            refreshSuggestions();
          }).catch(function () {});
        }
        // A sheet whose read takes this long is not read again before every question: the question uses
        // the last read, and the header says how old it is (a click reads it again). A six-tab workbook
        // spent five minutes reading before each question (2026-10-04); a small sheet reads in a second
        // or two and is always read fresh.
        var READ_REUSE_MS = 15000;
        var state = {conversationId: null, turns: [], history: [], tables: [], syncedFingerprint: '', ready: false,
          restored: false, sheetError: '', uid: null, signedIn: null, busy: false, live: null, scope: 'auto',
          loading: null, loaded: null, print: '', readMs: 0, syncedAt: 0, syncing: false};
        // The chat service's answer for a conversation deleted elsewhere (orchestrator/server.py).
        var NOT_FOUND = /conversation not found/i;

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
          return result.sheets.map(function (sheet) {
            return {name: sheet.name, data: sheet.csv, source: {kind: 'google-sheets-addon',
              warnings: (sheet.import && sheet.import.warnings || []).map(function (warning) { return warning.slice(0,4096); }),
              scope: {mode: state.scope, included: result.sheets.map(function (s) { return s.name; }),
                available: workbook.availableTabs || result.sheets.map(function (s) { return s.name; })}}};
          });
        }

        // A read of the sheet, made usable: imported, fingerprinted and timed. The header's sync time is
        // the moment the cells were read, which is the data the next question uses.
        async function takeWorkbook(workbook, startedAt) {
          var readMs = Date.now() - startedAt;
          await nextFrame();                 // let the status repaint before the import's busy work
          var importStarted = Date.now();
          var tables = importGrids(workbook);
          var print = await fingerprint(tables);
          state.tables = tables;
          state.print = print;
          state.readMs = readMs;
          state.syncedAt = startedAt + readMs;
          console.info('[prereasoner] sheet read ' + (readMs / 1000).toFixed(1) + ' s, prepared ' +
            ((Date.now() - importStarted) / 1000).toFixed(1) + ' s, ' + tables.length + ' tab(s)');
          return tables;
        }

        // Read the sheet again (before a question, or on the header's click), shown as syncing.
        async function readSheet() {
          state.syncing = true;
          renderSync();
          try {
            var startedAt = Date.now();
            return await takeWorkbook(await callServer('getWorkbookGrids', {scope: state.scope}), startedAt);
          } finally {
            state.syncing = false;
            renderSync();
          }
        }

        function nextFrame() {
          return new Promise(function (resolve) { window.setTimeout(resolve, 0); });
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
          var steps = turn.steps || [];
          if (!steps.length) return '';
          var url = live ? '' : analysisUrl(turn);
          var tree = R.renderReasoningTree(steps, {renderStep: function (step, index) {
            return R.renderStepLink(step, index, url ? {href: url, title: 'Open this reasoning in Prereasoner'} : {});
          }});
          var name = turn.analysis ? R.analysisName(turn.analysis) : '';
          return R.renderReasoningPanel({
            title: name ? 'Reasoning steps for ' + name : 'Reasoning steps',
            bodyHtml: tree, analysisUrl: url, open: !!live, className: live ? 'live-reasoning' : ''
          });
        }

        // What the engine read, when it differs from what was typed (a follow-up made whole, or a question
        // Gemini reworded so the search could read it): the line the live "Reading as" status becomes.
        // It was appended to the answer as "Gemini reworded the question as: ..." (2026-10-04).
        function turnHtml(turn) {
          if (turn.error) return failedHtml(turn);
          var reading = R.renderAsks(turn.asks || [], turn.question);
          return R.renderTurn({question: turn.question, assistantHtml: (reading ? '<div class="turn-reading">' + reading + '</div>' : '') +
            R.renderAssistantTurn({reasoningHtml: reasoningHtml(turn, false), reply: turn.reply || 'No answer was returned.',
              afterHtml: R.renderResultLink(turn.reply, analysisUrl(turn))})});
        }

        // Time since `since`, once it is long enough to reassure ("· 1:42"), filled in by `tick`.
        function elapsedHtml(since) {
          return '<span class="elapsed" data-since="' + Number(since) + '"></span>';
        }

        // What the sidebar is doing while it reads the sheet, from the first moment. A six-tab workbook left
        // the sidebar blank for ten minutes, and the user took it for broken (2026-10-04).
        function loadingHtml(loading) {
          var text = loading.stage === 'reading'
            ? (loading.tabs ? 'Reading ' + loading.tabs + (loading.tabs === 1 ? ' tab…' : ' tabs…') : 'Reading your spreadsheet…')
            : loading.stage === 'preparing' ? 'Preparing your sheets…' : 'Restoring your conversation…';
          return '<div class="loading" role="status"><div class="statusline"><span class="spin"></span>' +
            '<span>' + R.escapeHtml(text) + '</span>' + elapsedHtml(loading.startedAt) + '</div>' +
            '<div class="loading-hint" data-after="' + (loading.startedAt + 20000) + '" hidden>' +
            'Large spreadsheets take a few minutes to read.</div></div>';
        }

        function ago(ms) {
          var minutes = Math.floor(ms / 60000);
          if (minutes < 1) return 'just now';
          if (minutes < 60) return minutes + (minutes === 1 ? ' min ago' : ' mins ago');
          var hours = Math.floor(minutes / 60);
          if (hours < 24) return hours + (hours === 1 ? ' hour ago' : ' hours ago');
          var days = Math.floor(hours / 24);
          return days + (days === 1 ? ' day ago' : ' days ago');
        }

        // The header's right side: whether the sheet is being read, or how old the read the next question
        // uses is. A click reads the sheet again.
        function renderSync() {
          var text = state.syncing ? 'Syncing…' : state.syncedAt ? 'Synced ' + ago(Date.now() - state.syncedAt) : '';
          syncEl.textContent = text;
          syncEl.hidden = !text;
          syncEl.dataset.state = state.syncing ? 'syncing' : 'synced';
          syncEl.title = state.syncing ? 'Reading the spreadsheet' : 'Sync now';
          syncEl.disabled = state.syncing || state.busy || !!state.loading;
        }

        function tick() {
          var now = Date.now();
          Array.prototype.forEach.call(document.querySelectorAll('[data-since]'), function (element) {
            var seconds = Math.max(0, Math.floor((now - Number(element.dataset.since)) / 1000));
            element.textContent = seconds >= 5
              ? ' · ' + Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0') : '';
          });
          Array.prototype.forEach.call(document.querySelectorAll('[data-after]'), function (element) {
            element.hidden = now < Number(element.dataset.after);
          });
        }

        function liveHtml(live) {
          return R.renderTurn({question: live.question, assistantHtml: '<div class="turn-content">' +
            '<div class="statusline"><span class="spin"></span>' + R.escapeHtml(live.status) +
            elapsedHtml(live.startedAt) + '</div>' +
            reasoningHtml(live, true) +
            (live.reply ? '<div class="turn-answer convmsg answer">' + R.renderMarkdown(live.reply) + '</div>' : '') +
            '</div>'});
        }

        function failedHtml(failed) {
          var error = '<div class="turn-content"><div class="answer error" role="alert">' + R.escapeHtml(failed.error) + '</div></div>';
          return failed.question ? R.renderTurn({question: failed.question, assistantHtml: error}) : error;
        }

        function scrollToEnd() {
          window.requestAnimationFrame(function () { scrollEl.scrollTop = scrollEl.scrollHeight; });
        }

        // An empty sidebar is the composer and three starter questions, under the reading status while the
        // sheet is read. A chat adds the header: New chat, and the sheet's sync time.
        function render() {
          var html = state.turns.map(turnHtml).join('');
          if (state.live) html += '<div id="liveTurn">' + liveHtml(state.live) + '</div>';
          var empty = !html;
          if (empty && state.sheetError) html = '<div class="empty sheet-error">' + R.escapeHtml(state.sheetError) + '</div>';
          else if (empty && state.loading) html = loadingHtml(state.loading);
          threadEl.innerHTML = html;
          toplineEl.hidden = !state.turns.length;
          renderSync();
          suggestions.setActive(!state.turns.length && !state.live && !state.sheetError);
          tick();
          scrollToEnd();
        }

        // A failed question stays in the thread, with the reason in place of an answer.
        function keepFailed(question, error) {
          state.turns = state.turns.concat([{question: question, error: errorText(error)}]).slice(-24);
        }

        // A chat deleted elsewhere: the next question starts a new one, and the turns on screen and the
        // history the assistant reads stay.
        function forgetConversation() {
          state.conversationId = null;
          state.syncedFingerprint = '';
        }

        // A live event repaints only the running turn, so earlier turns keep their open panels.
        function renderLive() {
          var element = document.getElementById('liveTurn');
          if (!element || !state.live) return render();
          element.innerHTML = liveHtml(state.live);
          tick();
          scrollToEnd();
        }

        function setBusy(busy) {
          state.busy = busy;
          // Reading and answering must never lock the user's draft, and a question asked while the sheet
          // is still being read waits for that read.
          questionEl.disabled = false;
          sendEl.disabled = busy;
          newEl.disabled = busy || !state.ready;
          sendEl.textContent = busy ? '…' : '↑';
          renderSync();
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

        // The sheet's saved chat. A chat deleted elsewhere comes back empty. A failed read is tried again
        // with the next question, which fails rather than start a new chat over one it could not read:
        // treating a passing outage as an expired chat wiped saved conversations (2026-10-04).
        async function restore(tables, strict) {
          try {
            var restored = await callServer('restorePrereasonerSheetConversation', {tables: tables});
            state.conversationId = restored.conversationId || null;
            state.turns = restoredTurns(restored.state);
            state.history = restored.state && Array.isArray(restored.state.history) ? restored.state.history.slice(-24) : [];
            state.syncedFingerprint = (restored.state && restored.state.syncedFingerprint) || '';
            state.restored = true;
          } catch (error) {
            if (strict) throw error;
          }
        }

        async function submit() {
          var question = questionEl.value.trim();
          if (!question || state.busy) return;
          questionEl.value = '';
          setBusy(true);
          var live = {question: question, turnId: randomId(), startedAt: Date.now(), steps: [], asks: [], reply: '',
            status: state.loading ? 'Waiting for the sheet to finish reading…' : 'Reading the sheet…'};
          state.live = live;
          render();
          var stopLive = function () {};
          try {
            if (state.loaded) await state.loaded;          // the read that opened the sidebar
            // A sheet that reads quickly is read again, so an edit since then is in the answer; a slow one
            // uses its last read, whose age the header shows.
            var tables = state.tables.length && state.readMs >= READ_REUSE_MS ? state.tables : null;
            if (!tables) {
              live.status = 'Reading the sheet…';
              renderLive();
              tables = await readSheet();
            }
            var print = state.print;
            state.sheetError = '';
            if (!state.restored) await restore(tables, true);
            if (state.conversationId && state.syncedFingerprint && print !== state.syncedFingerprint) {
              live.status = 'Syncing the changed sheet…';
              renderLive();
              try {
                await callServer('syncPrereasonerConversation', {conversationId: state.conversationId, tables: tables});
              } catch (error) {
                if (!NOT_FOUND.test(errorText(error))) throw error;
                forgetConversation();
              }
            }
            live.status = 'Understanding your question…';
            renderLive();
            if (state.signedIn) await Promise.race([state.signedIn.catch(function () {}),
              new Promise(function (resolve) { window.setTimeout(resolve, 5000); })]);
            stopLive = startLive(live);
            var ask = function () {
              return callServer('askPrereasoner', {question: question, tables: tables,
                conversationId: state.conversationId, history: state.history, turnId: live.turnId});
            };
            var response;
            try {
              response = await ask();
            } catch (error) {
              if (state.conversationId && NOT_FOUND.test(errorText(error))) {
                forgetConversation();
                response = await ask();
              } else if (live.done && live.reply) {   // the live trace already finished this turn
                response = {reply: live.reply, conversationId: live.conversationId, history: [], traces: null};
              } else {
                throw error;
              }
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
            // Kept for this sheet only once the turn belongs to a conversation. A failed save loses
            // nothing on screen; the next answer saves the whole sidebar again.
            if (state.conversationId) {
              callServer('savePrereasonerSheetConversation', {conversationId: state.conversationId, state: snapshot()})
                .catch(function () {});
            }
          } catch (error) {
            state.live = null;
            keepFailed(question, error);
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
            render();
          } catch (error) {
            keepFailed('', error);
            render();
          } finally {
            setBusy(false);
          }
        });

        syncEl.addEventListener('click', async function () {
          if (state.busy || state.syncing || state.loading) return;
          try {
            await readSheet();
            state.sheetError = '';
          } catch (error) {
            keepFailed('', error);
          }
          render();
        });

        // Opening: the reading status at once, the starters as soon as the headers are read, and the cells,
        // the sign-in and this sheet's saved chat after them.
        setBusy(false);
        state.loading = {stage: 'reading', startedAt: Date.now(), tabs: 0};
        state.syncing = true;
        render();
        window.setInterval(function () { tick(); renderSync(); }, 1000);
        readSuggestionMetadata();            // headers only: starts before the full read
        var contextStarted = Date.now();
        state.loaded = callServer('getSidebarContext').then(async function (context) {
          state.signedIn = signIn(context.token);
          state.signedIn.catch(function () {});
          state.loading.stage = 'preparing';
          render();
          try {
            await takeWorkbook(context.workbook, contextStarted);
          } catch (error) {
            state.sheetError = errorText(error);
            return;
          }
          // Independent of restore/answers. A slow or unavailable suggestion never blocks typing.
          refreshSuggestions();
          state.loading.stage = 'restoring';
          render();
          await restore(state.tables);
        }).catch(function (error) {
          state.sheetError = errorText(error);
        }).then(function () {
          state.loading = null;
          state.loaded = null;
          state.syncing = false;
          state.ready = true;
          render();
          setBusy(state.busy);               // a question asked while reading is still being answered
          if (!state.busy) questionEl.focus();
        });
      })();
