/* ==========================================================================
   table.js -- client-side search over an already-rendered table, plus the
   staff-only account actions on the Users page.

   The search filters the rows currently on the page. Server-side filtering
   remains available through the querystring, which is what the filter
   dropdowns use, so results are still correct across pagination.
   ========================================================================== */

(function (App) {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {

    /* --- Live row filter --------------------------------------------------- */

    App.qsa('[data-table-search]').forEach(function (input) {
      var table = document.querySelector(input.dataset.tableSearch);
      if (!table) { return; }

      var rows = App.qsa('tbody tr', table);
      var counter = input.dataset.countTarget
        ? document.querySelector(input.dataset.countTarget) : null;
      var emptyRow = table.querySelector('[data-empty-row]');

      input.addEventListener('input', App.debounce(function () {
        var query = input.value.trim().toLowerCase();
        var shown = 0;

        rows.forEach(function (row) {
          if (row.hasAttribute('data-empty-row')) { return; }
          var match = !query || row.textContent.toLowerCase().indexOf(query) !== -1;
          row.hidden = !match;
          if (match) { shown += 1; }
        });

        if (counter) { counter.textContent = shown; }
        if (emptyRow) { emptyRow.hidden = shown > 0; }
      }, 150));
    });

    /* --- Account actions (staff only) -------------------------------------- */

    App.on('change', '[data-toggle-user-active]', function () {
      var input = this;
      var wanted = input.checked;

      App.postJSON(input.dataset.toggleUrl, {
        user_id: input.dataset.toggleUserActive,
        active: wanted
      })
        .then(function (result) {
          input.checked = result.is_active;
          App.toast(result.message, 'success');
          var pill = document.querySelector('[data-user-status="' + result.user_id + '"]');
          if (pill) {
            pill.className = 'badge badge--' + (result.is_active ? 'success' : 'danger');
            pill.textContent = result.is_active ? 'Active' : 'Suspended';
          }
        })
        .catch(function (error) {
          input.checked = !wanted;      // revert: the write did not happen
          App.toast(error.message, 'danger');
        });
    });

    App.on('change', '[data-toggle-user-staff]', function () {
      var input = this;
      var wanted = input.checked;

      App.postJSON(input.dataset.toggleUrl, {
        user_id: input.dataset.toggleUserStaff,
        is_staff: wanted
      })
        .then(function (result) {
          input.checked = result.is_staff;
          App.toast(result.message, 'success');
        })
        .catch(function (error) {
          input.checked = !wanted;
          App.toast(error.message, 'danger');
        });
    });
  });
})(window.App);
