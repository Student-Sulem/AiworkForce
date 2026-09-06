/* ==========================================================================
   approvals.js -- the Approvals page.

   Approve and reject are real <form method="post"> submissions, so the page
   works with JavaScript switched off. This file adds three conveniences on
   top: bulk selection, a rejection dialog that insists on a reason before it
   will submit, and a confirmation step on approve.
   ========================================================================== */

(function (App) {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {

    /* --- Rejection dialog -------------------------------------------------- */

    App.on('click', '[data-reject]', function (event) {
      event.preventDefault();
      var idField = document.getElementById('rejectApprovalId');
      var titleField = document.getElementById('rejectApprovalTitle');
      var reason = document.getElementById('rejectReason');

      if (idField) { idField.value = this.dataset.reject; }
      if (titleField) { titleField.textContent = this.dataset.title || 'this item'; }
      if (reason) { reason.value = ''; }

      App.openModal('rejectModal');
      if (reason) { reason.focus(); }
    });

    // Client-side mirror of ApprovalDecisionForm.clean(). The server rule is
    // the one that counts; this just avoids a wasted round trip.
    var rejectForm = document.getElementById('rejectForm');
    if (rejectForm) {
      rejectForm.addEventListener('submit', function (event) {
        var reason = document.getElementById('rejectReason');
        if (reason && !reason.value.trim()) {
          event.preventDefault();
          App.toast('A reason is required when rejecting an item.', 'warning');
          reason.focus();
        }
      });
    }

    /* --- Bulk selection ---------------------------------------------------- */

    var selectAll = document.getElementById('selectAllApprovals');
    var bulkBar = document.getElementById('bulkBar');
    var bulkCount = document.getElementById('bulkCount');

    function selectedIds() {
      return App.qsa('input[data-approval-select]:checked')
        .map(function (input) { return parseInt(input.value, 10); });
    }

    function refreshBulkBar() {
      var ids = selectedIds();
      if (bulkCount) { bulkCount.textContent = ids.length; }
      if (bulkBar) { bulkBar.hidden = ids.length === 0; }
    }

    App.on('change', 'input[data-approval-select]', refreshBulkBar);

    if (selectAll) {
      selectAll.addEventListener('change', function () {
        App.qsa('input[data-approval-select]').forEach(function (input) {
          input.checked = selectAll.checked;
        });
        refreshBulkBar();
      });
    }

    App.on('click', '[data-bulk-decision]', function (event) {
      event.preventDefault();
      var button = this;
      var decision = button.dataset.bulkDecision;
      var ids = selectedIds();

      if (!ids.length) {
        App.toast('Select at least one item first.', 'warning');
        return;
      }

      var reason = '';
      if (decision === 'rejected') {
        reason = window.prompt('Why are these items being rejected?') || '';
        if (!reason.trim()) {
          App.toast('A reason is required when rejecting items.', 'warning');
          return;
        }
      } else if (!window.confirm('Approve ' + ids.length + ' items? This publishes them.')) {
        return;
      }

      App.setLoading(button, true, 'Applying');

      App.postJSON(button.dataset.bulkUrl, {
        approval_ids: ids,
        decision: decision,
        reason: reason
      })
        .then(function (result) {
          App.toast(result.message, 'success');
          window.setTimeout(function () { window.location.reload(); }, 800);
        })
        .catch(function (error) {
          App.setLoading(button, false);
          App.toast(error.message, 'danger');
        });
    });

    refreshBulkBar();
  });
})(window.App);
