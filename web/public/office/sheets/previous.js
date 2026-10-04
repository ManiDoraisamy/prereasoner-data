var INITIAL_DATA = window.PrereasonerShell.initialData;
      var REASON_BASE = window.PrereasonerShell.reasonBase;
      (function () {
        var list = document.getElementById('list');
        function esc(value) {
          var node = document.createElement('div');
          node.textContent = value == null ? '' : String(value);
          return node.innerHTML;
        }
        function timestamp(value) {
          if (!value) return '';
          try { return new Date(value).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}); }
          catch (_) { return ''; }
        }
        function render(data) {
          list.innerHTML = '';
          if (!data || data.error) {
            list.className = 'status error';
            list.textContent = data && data.error ? data.error : 'Could not load previous conversations.';
            return;
          }
          var conversations = Array.isArray(data.conversations) ? data.conversations : [];
          if (!conversations.length) {
            list.className = 'status';
            list.textContent = 'Your previous conversations will appear here.';
            return;
          }
          conversations.forEach(function (conversation) {
            var id = String(conversation.id || '');
            if (!/^c_[0-9a-f]{32}$/i.test(id)) return;
            var link = document.createElement('a');
            link.className = 'conversation';
            link.href = REASON_BASE + encodeURIComponent(id);
            link.target = '_blank';
            link.rel = 'noopener';
            link.innerHTML = '<div class="question">' + esc(conversation.question || '(untitled)') + '</div>' +
              (conversation.ts ? '<div class="time">' + esc(timestamp(conversation.ts)) + '</div>' : '');
            list.appendChild(link);
          });
        }
        render(INITIAL_DATA);
      }());
