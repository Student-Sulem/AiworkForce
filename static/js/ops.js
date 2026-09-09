/* ==========================================================================
   ops.js -- the operational pages.

   Five behaviours, all of them data-attribute driven and all of them optional:
   every page here works read-only with JavaScript switched off, because the
   filters are GET forms and the boards are plain links.

     1. knowledge search      posts to api_knowledge_search and renders hits
     2. add a document        posts to api_knowledge_index
     3. sync a source         posts to api_knowledge_sync
     4. inline status control posts to api_ops_update, updates the cell
     5. board column filters  hides a stage column, client side only

   Conventions taken from the existing modules: App from core.js for
   selection, delegation, POSTing and the loading state; App.toast for
   feedback; the server's own message is what the user is shown on failure,
   because the server is the one that knows why it refused.
   ========================================================================== */

(function (App) {
  'use strict';

  /* Guard against being included twice.

     The pages here link this file from {% block extra_js %}, and base.html may
     also come to link it globally. Everything below binds through delegation
     on `document`, so a second execution would attach a second handler and
     every inline status change would POST twice. Once is enough. */
  if (App.__opsLoaded) { return; }
  App.__opsLoaded = true;

  /* --- Small helpers ---------------------------------------------------- */

  /* Escape a string for use inside a regular expression. The matched terms
     come from the index and can contain a hyphen or a full stop. */
  function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  /* Mark the query terms inside an already-escaped snippet.

     The order matters and is the whole reason this is two steps: the snippet
     is escaped FIRST with App.escapeHtml, so nothing in the document body can
     inject markup, and only then are <mark> tags introduced around plain-text
     matches. Marking before escaping would escape the marks. */
  function highlight(snippet, terms) {
    var html = App.escapeHtml(snippet);
    (terms || []).forEach(function (term) {
      var text = String(term || '').trim();
      if (text.length < 2) { return; }
      var pattern = new RegExp('(' + escapeRegExp(text) + ')', 'gi');
      html = html.replace(pattern, '<mark>$1</mark>');
    });
    return html;
  }

  function setBusy(element, isBusy) {
    if (!element) { return; }
    element.disabled = !!isBusy;
    element.setAttribute('aria-busy', isBusy ? 'true' : 'false');
  }


  /* ======================================================================
     1 and 2. Knowledge search, and its results
     ====================================================================== */

  function renderHits(container, payload) {
    var results = payload.results || [];

    if (!results.length) {
      container.innerHTML =
        '<p class="u-text-sm u-text-muted u-m-0">' +
        App.escapeHtml(payload.message ||
          'Nothing in the knowledge base matched that.') +
        '</p>';
      return;
    }

    container.innerHTML = results.map(function (hit) {
      var heading = hit.heading
        ? '<span class="chip">' + App.escapeHtml(hit.heading) + '</span>' : '';
      var department = hit.department
        ? '<span class="chip">' + App.escapeHtml(hit.department) + '</span>' : '';
      var title = hit.url
        ? '<a href="' + App.escapeHtml(hit.url) + '">' + App.escapeHtml(hit.title) + '</a>'
        : App.escapeHtml(hit.title);

      return '' +
        '<article class="hit">' +
          '<div class="hit__head">' +
            '<h3 class="hit__title">' + title + '</h3>' +
            '<span class="badge badge--info">' +
              App.escapeHtml(hit.doc_type_label || 'Other') + '</span>' +
            heading + department +
          '</div>' +
          '<div class="hit__score">' +
            '<span class="hit__bar" role="img" aria-label="Relevance ' +
              App.escapeHtml(String(hit.bar)) + ' per cent of the best match">' +
              '<span style="width:' + Math.max(2, Math.min(100, hit.bar)) + '%"></span>' +
            '</span>' +
            '<span class="hit__figure">' + App.escapeHtml(String(hit.score)) + '</span>' +
          '</div>' +
          '<p class="hit__snippet">' + highlight(hit.snippet, hit.matched_terms) + '</p>' +
          '<p class="hit__why"><i class="fa-solid fa-circle-info" aria-hidden="true"></i> ' +
            App.escapeHtml(hit.why || 'No explanation was recorded for this match.') +
          '</p>' +
        '</article>';
    }).join('');
  }

  function initKnowledgeSearch() {
    var form = document.getElementById('knowledgeSearchForm');
    if (!form) { return; }

    var input = document.getElementById('knowledgeQuery');
    var typeField = document.getElementById('knowledgeSearchType');
    var results = document.getElementById('knowledgeResults');
    var summary = document.getElementById('knowledgeResultsSummary');
    var button = form.querySelector('[type="submit"]');

    function runSearch() {
      var query = (input.value || '').trim();
      if (!query) {
        results.innerHTML = '';
        if (summary) { summary.textContent = ''; }
        return;
      }

      App.setLoading(button, true, 'Searching');

      App.postJSON(form.dataset.searchUrl, {
        query: query,
        doc_type: typeField ? typeField.value : '',
        limit: 10
      })
        .then(function (payload) {
          App.setLoading(button, false);
          renderHits(results, payload);
          if (summary) { summary.textContent = payload.message || ''; }
        })
        .catch(function (error) {
          App.setLoading(button, false);
          results.innerHTML = '';
          if (summary) { summary.textContent = error.message; }
          App.toast(error.message, 'danger');
        });
    }

    // A real submit as well as the debounced typing, so Enter works and so a
    // deliberate press always runs even when nothing changed.
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      runSearch();
    });

    input.addEventListener('input', App.debounce(function () {
      if ((input.value || '').trim().length >= 3) { runSearch(); }
    }, 350));

    if (typeField) { typeField.addEventListener('change', runSearch); }
  }


  /* ======================================================================
     3. Add a document
     ====================================================================== */

  function initAddDocument() {
    var form = document.getElementById('addDocumentForm');
    if (!form) { return; }

    var button = form.querySelector('[type="submit"]');

    form.addEventListener('submit', function (event) {
      event.preventDefault();

      var title = (form.elements.title.value || '').trim();
      var content = (form.elements.content.value || '').trim();

      // Mirrors the server's own rule so an obvious mistake costs no round
      // trip. The server check is still the one that counts.
      if (!title) {
        App.toast('Give the document a title.', 'warning');
        form.elements.title.focus();
        return;
      }
      if (content.split(/\s+/).filter(Boolean).length < 10) {
        App.toast('The document needs at least ten words to be worth indexing.',
                  'warning');
        form.elements.content.focus();
        return;
      }

      App.setLoading(button, true, 'Indexing');

      App.postJSON(form.dataset.indexUrl, {
        title: title,
        content: content,
        doc_type: form.elements.doc_type.value,
        department: (form.elements.department.value || '').trim(),
        tags: (form.elements.tags.value || '').trim()
      }, 30000)
        .then(function (payload) {
          App.setLoading(button, false);
          App.toast(payload.message, 'success');
          form.reset();
          // Reload so the document list, the stats and the type counts all
          // reflect the new row rather than only two of the three.
          window.setTimeout(function () { window.location.reload(); }, 900);
        })
        .catch(function (error) {
          App.setLoading(button, false);
          App.toast(error.message, 'danger');
        });
    });
  }


  /* ======================================================================
     4. Sync one knowledge source
     ====================================================================== */

  function initSourceSync() {
    App.on('click', '[data-sync-source]', function (event) {
      event.preventDefault();
      var button = this;
      var row = document.querySelector(
        '[data-source-row="' + button.dataset.syncSource + '"]');

      App.setLoading(button, true, 'Syncing');

      App.postJSON(button.dataset.syncUrl, {
        source_id: parseInt(button.dataset.syncSource, 10)
      }, 40000)
        .then(function (payload) {
          App.setLoading(button, false);
          App.toast(payload.message, 'success');
          if (row) {
            var when = row.querySelector('[data-source-synced]');
            var note = row.querySelector('[data-source-message]');
            var count = row.querySelector('[data-source-count]');
            if (when) { when.textContent = payload.last_synced; }
            if (note) { note.textContent = payload.message; }
            if (count) { count.textContent = payload.document_count; }
          }
        })
        .catch(function (error) {
          App.setLoading(button, false);
          App.toast(error.message, 'danger');
        });
    });
  }


  /* ======================================================================
     5. The inline status, priority and assignee controls

     The select is the control and the source of truth for what the user asked
     for. On failure it is put back to the value the server still holds, so the
     page never shows a change that did not happen.
     ====================================================================== */

  function initInlineUpdates() {
    App.on('change', '[data-ops-field]', function () {
      var select = this;
      var previous = select.dataset.opsCurrent || '';
      var wanted = select.value;

      if (wanted === previous) { return; }

      setBusy(select, true);

      App.postJSON(select.dataset.opsUrl, {
        model: select.dataset.opsModel,
        id: parseInt(select.dataset.opsId, 10),
        field: select.dataset.opsField,
        value: wanted
      })
        .then(function (payload) {
          setBusy(select, false);
          select.dataset.opsCurrent = payload.value == null ? '' : String(payload.value);
          App.toast(payload.message, 'success');

          // Replace the rendered value in place. The target is named by the
          // control rather than guessed from the DOM, because the same
          // endpoint serves a table cell, a board card and a detail panel.
          var target = select.dataset.opsDisplay
            ? document.querySelector(select.dataset.opsDisplay) : null;
          if (target) {
            target.textContent = payload.display;
            target.classList.add('is-updated');
            window.setTimeout(function () {
              target.classList.remove('is-updated');
            }, 1200);
          }
        })
        .catch(function (error) {
          setBusy(select, false);
          select.value = previous;      // the write did not happen
          App.toast(error.message, 'danger');
        });
    });
  }


  /* ======================================================================
     6. Board column filters

     Purely presentational and purely client side: hiding the Done column does
     not change what the server returned, so there is nothing to persist and
     nothing to get out of step.
     ====================================================================== */

  function initBoardFilters() {
    App.on('click', '[data-board-filter]', function (event) {
      event.preventDefault();
      var button = this;
      var board = document.querySelector(button.dataset.boardTarget);
      if (!board) { return; }

      var stage = button.dataset.boardFilter;
      var pressed = button.getAttribute('aria-pressed') === 'true';

      if (stage === 'all') {
        App.qsa('.board__column', board).forEach(function (column) {
          column.hidden = false;
        });
        App.qsa('[data-board-filter][data-board-target="' +
                button.dataset.boardTarget + '"]').forEach(function (other) {
          other.setAttribute('aria-pressed', other === button ? 'true' : 'false');
        });
        return;
      }

      var column = board.querySelector('[data-stage="' + stage + '"]');
      if (column) { column.hidden = pressed; }
      button.setAttribute('aria-pressed', pressed ? 'false' : 'true');

      var allButton = document.querySelector(
        '[data-board-filter="all"][data-board-target="' +
        button.dataset.boardTarget + '"]');
      if (allButton) { allButton.setAttribute('aria-pressed', 'false'); }
    });
  }


  /* --- Boot ------------------------------------------------------------- */

  document.addEventListener('DOMContentLoaded', function () {
    initKnowledgeSearch();
    initAddDocument();
    initSourceSync();
    initInlineUpdates();
    initBoardFilters();
  });
})(window.App);
