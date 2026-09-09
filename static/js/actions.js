/* ==========================================================================
   actions.js -- the approval queue, the review screen, and the orchestrator
   console.

   Loaded by actions.html, action_detail.html and orchestrator.html. Each block
   below binds only if the elements it needs are on the page, so the same file
   serves all three without any of them paying for the others.

   WHY DECIDING IS A fetch() AND FILTERING IS NOT
   The filters and the status tabs on actions.html are real links and a real
   GET form, so narrowing the queue never depends on JavaScript. Deciding is a
   POST from here instead, because a decision on row nineteen of a long queue
   should not throw the reviewer back to the top of the page. The row is
   updated where it sits.

   WHERE A RELOAD IS STILL RIGHT
   A bulk decision changes many rows, several counters and possibly the page
   the reviewer is on, and the review screen's own timeline, payload and
   outcome all change together. Reconstructing either in JavaScript would mean
   maintaining a second copy of the templates, so those reload. A single
   decision in the queue does not.

   Everything goes through App from core.js: App.postJSON adds the CSRF header,
   App.setLoading disables a button while its request is in flight, and
   App.toast reports the outcome -- the server's own sentence on failure, never
   an invented one.
   ========================================================================== */

(function (App) {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {

    /* --- Shared: the two dialogs ---------------------------------------- */

    // The button that opened a dialog, so the dialog's own confirm knows what
    // it is confirming. Held here rather than on the dialog because the same
    // dialog serves every row.
    var pendingButton = null;

    function textOf(id, value) {
      var element = document.getElementById(id);
      if (element) { element.textContent = value; }
    }

    /* --- Deciding one action -------------------------------------------- */

    /* Approve: a high-risk action asks once more before it goes, because a
       one-click send to somebody outside the company is not a review. A demo
       integration is confirmed too, so nobody believes they sent something
       they only simulated. */
    App.on('click', '[data-decide="approved"]', function (event) {
      event.preventDefault();
      var button = this;
      var risk = button.dataset.risk || '';
      var isDemo = button.dataset.demo === '1';

      if (risk !== 'high' && !isDemo) {
        decide(button, 'approved', '');
        return;
      }

      pendingButton = button;
      textOf('actionConfirmSubject', '"' + (button.dataset.title || 'This action') + '"');
      textOf('actionConfirmMessage', isDemo
        ? 'The application this goes through is in demo mode, so approving it '
          + 'will simulate the action and record it. Nothing will actually be sent.'
        : 'This action reaches somebody outside the company. Once approved it '
          + 'is carried out immediately and cannot be recalled.');

      var proceed = document.getElementById('actionConfirmProceed');
      if (proceed) {
        proceed.innerHTML = isDemo
          ? '<i class="fa-solid fa-flask" aria-hidden="true"></i> Approve and simulate'
          : '<i class="fa-solid fa-check" aria-hidden="true"></i> Approve and execute';
      }
      App.openModal('actionConfirmModal');
    });

    var confirmProceed = document.getElementById('actionConfirmProceed');
    if (confirmProceed) {
      confirmProceed.addEventListener('click', function () {
        App.closeModal('actionConfirmModal');
        if (pendingButton) {
          decide(pendingButton, 'approved', '');
          pendingButton = null;
        }
      });
    }

    /* Reject: the reason is mandatory here and again in the endpoint. A
       rejection with no explanation tells the employee nothing. */
    App.on('click', '[data-decide="rejected"]', function (event) {
      event.preventDefault();
      pendingButton = this;
      textOf('actionRejectSubject', '"' + (this.dataset.title || 'this action') + '"');

      var reason = document.getElementById('actionRejectReason');
      if (reason) { reason.value = ''; }
      App.openModal('actionRejectModal');
      if (reason) { reason.focus(); }
    });

    var rejectConfirm = document.getElementById('actionRejectConfirm');
    if (rejectConfirm) {
      rejectConfirm.addEventListener('click', function () {
        var reason = document.getElementById('actionRejectReason');
        var text = reason ? reason.value.trim() : '';
        if (!text) {
          App.toast('A reason is required when rejecting an action.', 'warning');
          if (reason) { reason.focus(); }
          return;
        }
        App.closeModal('actionRejectModal');
        if (pendingButton) {
          decide(pendingButton, 'rejected', text);
          pendingButton = null;
        }
      });
    }

    function decide(button, decision, reason) {
      App.setLoading(button, true, decision === 'approved' ? 'Approving' : 'Rejecting');

      App.postJSON(button.dataset.url, {
        action_id: parseInt(button.dataset.actionId, 10),
        decision: decision,
        reason: reason
      }, 45000)
        .then(function (result) {
          App.toast(result.message, decision === 'approved' ? 'success' : 'info');
          updateBadge(result.pending_count);

          // The review screen changes in half a dozen places at once, so it
          // reloads rather than being patched from here.
          if (button.dataset.reload === '1') {
            window.setTimeout(function () { window.location.reload(); }, 700);
            return;
          }
          settleRow(result.action, button.dataset.retryUrl || '');
        })
        .catch(function (error) {
          App.setLoading(button, false);
          App.toast(error.message, 'danger');
        });
    }

    /* Replace a decided row's footer in place. The reviewer keeps their
       position in the queue, and the row now says what happened to it. */
    function settleRow(action, retryUrl) {
      if (!action) { window.location.reload(); return; }

      var row = document.querySelector('[data-action-row="' + action.id + '"]');
      if (!row) { return; }

      row.classList.add('queue-row--settled');
      row.classList.remove('queue-row--overdue');

      var check = row.querySelector('input[data-action-select]');
      if (check) { check.checked = false; check.remove(); }
      refreshBulkBar();

      var footer = row.querySelector('.card__footer');
      if (footer) {
        // The retry endpoint's URL comes from the button that was pressed, so
        // this file never hard-codes a path that urls.py owns.
        var retry = (action.status === 'failed' && retryUrl)
          ? '<button class="btn btn--secondary btn--sm" type="button" '
            + 'data-retry="' + action.id + '" data-url="' + retryUrl + '">'
            + '<i class="fa-solid fa-rotate-right" aria-hidden="true"></i> Retry</button>'
          : '';
        footer.innerHTML =
          retry
          + '<span class="u-text-sm u-text-muted" data-decision-summary>'
          + App.escapeHtml(action.decision_summary) + '</span>'
          + '<div class="u-grow"></div>'
          + '<a href="' + action.url + '" class="btn btn--ghost btn--sm">Open '
          + '<i class="fa-solid fa-chevron-right" aria-hidden="true"></i></a>';
      }

      var badge = row.querySelector('.badge__dot');
      if (badge && badge.parentElement) {
        badge.parentElement.className = 'badge badge--' + toneFor(action.status);
        badge.parentElement.innerHTML =
          '<span class="badge__dot" aria-hidden="true"></span>'
          + App.escapeHtml(action.status_label);
      }

      if (action.execution_summary) {
        var note = document.createElement('div');
        note.className = 'alert alert--'
          + (action.status === 'failed' ? 'danger'
            : (action.executed_in_demo ? 'info' : 'success'));
        note.innerHTML =
          '<i class="fa-solid fa-circle-info" aria-hidden="true"></i><span>'
          + (action.executed_in_demo ? '<strong>Simulated.</strong> ' : '')
          + App.escapeHtml(action.execution_summary) + '</span>';
        var body = row.querySelector('.card__body');
        if (body) { body.appendChild(note); }
      }
    }

    function toneFor(status) {
      if (status === 'executed' || status === 'approved') { return 'success'; }
      if (status === 'failed' || status === 'rejected') { return 'danger'; }
      if (status === 'pending' || status === 'executing') { return 'warning'; }
      return 'neutral';
    }

    function updateBadge(count) {
      if (typeof count !== 'number') { return; }
      var badge = document.getElementById('navActionBadge');
      if (badge) {
        badge.textContent = count;
        badge.hidden = count === 0;
      }
    }

    /* --- Retrying a failed execution ------------------------------------ */

    App.on('click', '[data-retry]', function (event) {
      event.preventDefault();
      var button = this;

      if (!window.confirm('Try this execution again? The decision is not '
        + 'revisited, only the execution.')) {
        return;
      }

      App.setLoading(button, true, 'Retrying');
      App.postJSON(button.dataset.url, {
        action_id: parseInt(button.dataset.retry, 10)
      }, 45000)
        .then(function (result) {
          App.toast(result.message, 'success');
          updateBadge(result.pending_count);
          window.setTimeout(function () { window.location.reload(); }, 700);
        })
        .catch(function (error) {
          App.setLoading(button, false);
          App.toast(error.message, 'danger');
        });
    });

    /* --- Bulk selection -------------------------------------------------- */

    var selectAll = document.getElementById('selectAllActions');
    var bulkBar = document.getElementById('actionBulkBar');
    var bulkCount = document.getElementById('actionBulkCount');

    function selectedIds() {
      return App.qsa('input[data-action-select]:checked').map(function (input) {
        return parseInt(input.value, 10);
      });
    }

    function refreshBulkBar() {
      var ids = selectedIds();
      if (bulkCount) { bulkCount.textContent = ids.length; }
      // The bar carries aria-live, so unhiding it announces the count.
      if (bulkBar) { bulkBar.hidden = ids.length === 0; }
    }

    App.on('change', 'input[data-action-select]', refreshBulkBar);

    if (selectAll) {
      selectAll.addEventListener('change', function () {
        App.qsa('input[data-action-select]').forEach(function (input) {
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
        App.toast('Select at least one action first.', 'warning');
        return;
      }

      var reason = '';
      if (decision === 'rejected') {
        reason = window.prompt('Why are these ' + ids.length
          + ' actions being rejected? The reason is recorded on every one.') || '';
        if (!reason.trim()) {
          App.toast('A reason is required when rejecting actions.', 'warning');
          return;
        }
      } else if (!window.confirm('Approve ' + ids.length + ' action'
        + (ids.length === 1 ? '' : 's')
        + '? Each one will be carried out immediately.')) {
        return;
      }

      App.setLoading(button, true, 'Applying');

      App.postJSON(button.dataset.bulkUrl, {
        action_ids: ids,
        decision: decision,
        reason: reason.trim()
      }, 90000)
        .then(function (result) {
          App.toast(result.message, 'success');
          updateBadge(result.pending_count);
          window.setTimeout(function () { window.location.reload(); }, 800);
        })
        .catch(function (error) {
          App.setLoading(button, false);
          App.toast(error.message, 'danger');
        });
    });

    refreshBulkBar();

    /* --- Saving payload edits, which does NOT approve -------------------- */

    var payloadForm = document.getElementById('payloadForm');
    if (payloadForm) {
      payloadForm.addEventListener('submit', function (event) {
        event.preventDefault();

        var button = document.getElementById('saveEditsButton');
        var changes = {};
        var count = 0;

        App.qsa('[data-payload-field]', payloadForm).forEach(function (control) {
          var key = control.dataset.payloadField;
          var type = control.dataset.fieldType;
          changes[key] = (type === 'boolean') ? control.checked : control.value;
          count += 1;
        });

        if (!count) {
          App.toast('There is nothing on this action that can be edited.', 'warning');
          return;
        }

        App.setLoading(button, true, 'Saving');

        App.postJSON(payloadForm.dataset.url, {
          action_id: parseInt(payloadForm.dataset.actionId, 10),
          changes: changes
        })
          .then(function (result) {
            App.toast(result.message, 'success');
            // Reload so the comparison panel and the audit trail show the
            // edit that was just recorded. Both are server-rendered, and the
            // whole point of the panel is that it is authoritative.
            window.setTimeout(function () { window.location.reload(); }, 900);
          })
          .catch(function (error) {
            App.setLoading(button, false);
            App.toast(error.message, 'danger');
          });
      });
    }

    /* --- The orchestrator console ---------------------------------------- */

    // Clicking an example fills the box rather than submitting, so a
    // first-time user can read what they are about to ask and change it.
    App.on('click', '[data-example]', function (event) {
      event.preventDefault();
      var input = document.getElementById('orchestratorInput');
      if (!input) { return; }
      input.value = this.dataset.example;
      input.focus();
      input.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });

    var orchestratorForm = document.getElementById('orchestratorForm');
    if (orchestratorForm) {
      orchestratorForm.addEventListener('submit', function (event) {
        event.preventDefault();

        var input = document.getElementById('orchestratorInput');
        var button = document.getElementById('orchestratorSubmit');
        var text = input ? input.value.trim() : '';

        if (!text) {
          App.toast('Describe what you need first.', 'warning');
          if (input) { input.focus(); }
          return;
        }

        App.setLoading(button, true, 'Routing');

        // Generously long: routing may consult a language model, and the
        // employee then does real work before answering.
        App.postJSON(orchestratorForm.dataset.url, { request_text: text }, 120000)
          .then(function (result) {
            App.setLoading(button, false);
            renderRouting(result.decision);
            renderResult(result.result);
            renderWorkflow(result.workflow);
            App.toast(result.message, 'success');

            var panel = document.getElementById('routingPanel');
            if (panel) { panel.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
          })
          .catch(function (error) {
            App.setLoading(button, false);
            App.toast(error.message, 'danger');
          });
      });
    }

    function renderRouting(decision) {
      var panel = document.getElementById('routingPanel');
      if (!panel || !decision) { return; }

      textOf('routedName', decision.agent_name || 'No employee was chosen');
      textOf('routedRole', decision.agent_role || '');
      textOf('routingReason', decision.reasoning
        || 'The router gave no reasoning for this choice.');
      textOf('routingMethod', decision.method
        ? ('decided by ' + decision.method) : '');

      var avatar = document.getElementById('routedAvatar');
      if (avatar) {
        avatar.innerHTML = '<i class="fa-solid ' + (decision.agent_icon || 'fa-robot') + '"></i>';
        avatar.style.background = decision.agent_color || '';
      }

      var link = document.getElementById('routedLink');
      if (link) {
        link.hidden = !decision.agent_url;
        if (decision.agent_url) { link.href = decision.agent_url; }
      }

      // The bar is workforce.css's shared .meter: the width comes from the
      // --value custom property, and the tone from a meter__fill modifier.
      var percent = Math.max(0, Math.min(100, decision.confidence || 0));
      textOf('confidenceValue', percent + '%');
      var track = document.getElementById('confidenceTrack');
      if (track) { track.style.setProperty('--value', percent + '%'); }
      var fill = document.getElementById('confidenceFill');
      if (fill) {
        fill.className = 'meter__fill meter__fill--'
          + (percent >= 70 ? 'success' : (percent >= 40 ? 'warning' : 'danger'));
      }

      var block = document.getElementById('candidatesBlock');
      var body = document.getElementById('candidatesBody');
      var rows = decision.candidates || [];
      if (block && body) {
        body.innerHTML = rows.map(function (row) {
          return '<tr>'
            + '<td data-label="Employee">' + App.escapeHtml(row.name) + '</td>'
            + '<td data-label="Score">' + App.escapeHtml(row.score) + '</td>'
            + '<td data-label="Why it scored" class="u-break">'
            + App.escapeHtml(row.reason || 'no reason recorded') + '</td>'
            + '</tr>';
        }).join('');
        block.hidden = rows.length === 0;
      }

      panel.hidden = false;
    }

    function renderResult(result) {
      var panel = document.getElementById('resultPanel');
      if (!panel) { return; }

      if (!result) { panel.hidden = true; return; }

      var reply = document.getElementById('resultReply');
      if (reply) {
        // textContent, not innerHTML: the reply is model output and is treated
        // as text everywhere it is not run through the server's own renderer.
        reply.textContent = result.reply || 'The employee returned no reply.';
      }

      var error = document.getElementById('resultError');
      if (error) {
        error.hidden = !result.error;
        textOf('resultErrorText', result.error || '');
      }

      var task = document.getElementById('resultTask');
      if (task) {
        task.hidden = !result.task_title;
        task.textContent = result.task_title
          ? (result.task_title + (result.task_status ? ' -- ' + result.task_status : ''))
          : '';
      }

      var tools = document.getElementById('resultTools');
      if (tools) {
        tools.innerHTML = (result.tools || []).map(function (name) {
          return '<span class="chip chip--mono">' + App.escapeHtml(name) + '</span>';
        }).join('');
      }

      var block = document.getElementById('resultActionsBlock');
      var list = document.getElementById('resultActions');
      var actions = result.actions || [];
      if (block && list) {
        list.innerHTML = actions.map(function (action) {
          return '<li class="proposed-list__item">'
            + '<span class="risk-pill risk-pill--' + App.escapeHtml(action.risk) + '">'
            + App.escapeHtml(action.risk_label || action.risk) + '</span>'
            + '<a class="u-grow u-truncate" href="' + action.url + '">'
            + App.escapeHtml(action.title) + '</a>'
            + '<span class="u-text-xs u-text-subtle">'
            + App.escapeHtml(action.target_app) + '</span>'
            + '</li>';
        }).join('');
        block.hidden = actions.length === 0;
      }

      panel.hidden = false;
    }

    function renderWorkflow(steps) {
      var panel = document.getElementById('workflowPanel');
      var list = document.getElementById('workflowSteps');
      if (!panel || !list) { return; }

      // The view already withholds a single-step "workflow", which would only
      // be the routing decision restated.
      if (!steps || !steps.length) { panel.hidden = true; return; }

      list.innerHTML = steps.map(function (step) {
        return '<li class="workflow-steps__item">'
          + '<span class="workflow-steps__who">'
          + '<i class="fa-solid ' + App.escapeHtml(step.icon || 'fa-robot') + '" aria-hidden="true"></i> '
          + (step.url
            ? '<a href="' + step.url + '">' + App.escapeHtml(step.name) + '</a>'
            : App.escapeHtml(step.name))
          + '</span>'
          + '<span class="workflow-steps__what">' + App.escapeHtml(step.description) + '</span>'
          + (step.reason
            ? '<span class="workflow-steps__why">' + App.escapeHtml(step.reason) + '</span>'
            : '')
          + '</li>';
      }).join('');

      panel.hidden = false;
    }
  });
})(window.App);
