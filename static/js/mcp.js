/* ==========================================================================
   mcp.js -- the MCP Tools page.

   Enabling or disabling a server updates the switch immediately and reverts
   it if the server rejects the change, so the control never shows a state the
   database does not actually hold.
   ========================================================================== */

(function (App) {
  'use strict';

  var TONE = {
    connected: 'success',
    degraded: 'warning',
    failed: 'danger',
    disabled: 'neutral',
    unknown: 'neutral'
  };

  function paintStatus(card, status, statusDisplay) {
    var pill = card ? card.querySelector('[data-status-pill]') : null;
    if (!pill) { return; }
    pill.className = 'badge badge--' + (TONE[status] || 'neutral');
    pill.textContent = statusDisplay;
  }

  document.addEventListener('DOMContentLoaded', function () {

    /* --- Enable / disable a server --------------------------------------- */

    App.on('change', '[data-toggle-server]', function () {
      var input = this;
      var card = input.closest('[data-server-card]');
      var wanted = input.checked;

      App.postJSON(input.dataset.toggleUrl, {
        server_id: input.dataset.toggleServer,
        enabled: wanted
      })
        .then(function (result) {
          input.checked = result.is_enabled;
          paintStatus(card, result.connection_status, result.status_display);
          App.toast(
            input.dataset.serverName + ' was ' + (result.is_enabled ? 'enabled' : 'disabled') + '.',
            result.is_enabled ? 'success' : 'info');
        })
        .catch(function (error) {
          input.checked = !wanted;      // revert: the write did not happen
          App.toast(error.message, 'danger');
        });
    });

    /* --- Run the handshake ----------------------------------------------- */

    App.on('click', '[data-test-server]', function (event) {
      event.preventDefault();
      var button = this;
      var card = button.closest('[data-server-card]');
      var result = card ? card.querySelector('[data-test-result]') : null;

      App.setLoading(button, true, 'Testing');
      if (result) {
        result.className = 'alert alert--info';
        result.hidden = false;
        result.textContent = 'Establishing a session...';
      }

      App.postJSON(button.dataset.testUrl, { server_id: button.dataset.testServer })
        .then(function (payload) {
          var tone = TONE[payload.connection_status] || 'info';
          if (result) {
            result.className = 'alert alert--' +
              (tone === 'neutral' ? 'info' : tone === 'success' ? 'success'
                : tone === 'warning' ? 'warning' : 'danger');
            result.textContent = payload.message;
          }
          paintStatus(card, payload.connection_status, payload.status_display);
          App.setLoading(button, false);
        })
        .catch(function (error) {
          if (result) {
            result.className = 'alert alert--danger';
            result.textContent = error.message;
          }
          App.setLoading(button, false);
        });
    });

    /* --- Filter by category ---------------------------------------------- */

    var categoryFilter = document.getElementById('mcpCategoryFilter');
    var searchInput = document.getElementById('mcpSearch');

    function applyFilters() {
      var category = categoryFilter ? categoryFilter.value : 'all';
      var query = searchInput ? searchInput.value.trim().toLowerCase() : '';
      var shown = 0;

      App.qsa('[data-server-card]').forEach(function (card) {
        var matchesCategory = category === 'all' || card.dataset.category === category;
        var matchesQuery = !query || card.textContent.toLowerCase().indexOf(query) !== -1;
        var visible = matchesCategory && matchesQuery;
        card.hidden = !visible;
        if (visible) { shown += 1; }
      });

      var empty = document.getElementById('mcpEmptyState');
      if (empty) { empty.hidden = shown > 0; }
    }

    if (categoryFilter) { categoryFilter.addEventListener('change', applyFilters); }
    if (searchInput) { searchInput.addEventListener('input', App.debounce(applyFilters, 150)); }
  });
})(window.App);
