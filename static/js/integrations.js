/* ==========================================================================
   integrations.js -- the Integrations page, the integration detail page and
   the Platform settings page.

   One file for three pages because they share one idea: a small change is
   posted as JSON, the server answers with the new truth, and the part of the
   page that showed the old truth is repainted from that answer rather than
   from what the browser hoped would happen. Nothing here optimistically
   assumes a write succeeded.

   THE QUEUED-FOR-APPROVAL CASE
   Configuring an integration and changing a setting are governed actions, so
   the server may answer {status: 'pending'} instead of 'success'. That is not
   an error and it is not a save: the change is sitting in the approval queue.
   Every handler below tells the person exactly that and links them to the
   queue, because reporting "Saved" over a pending change would be the single
   most misleading thing this file could do.

   No secret value passes through this file in either direction. A blank
   credential box is simply omitted from the request, which is what makes
   "leave it blank to keep the stored value" true.

   Depends on core.js (App), modal.js and toast.js, all loaded by base.html.
   No framework, no build step.
   ========================================================================== */

(function (App) {
  'use strict';

  /* The mode wording comes from the server, rendered into the page by
     json_script, so the sentences on a repainted card are the very same
     strings Django rendered on page load and the two cannot drift apart. */
  var MODE_META = App.readJSON('integrationModeMeta') || {};

  var FALLBACK_MODE = {
    label: 'Unknown',
    tone: 'neutral',
    icon: 'fa-circle-question',
    explanation: 'The current behaviour of this integration could not be read.'
  };

  function modeMeta(mode) {
    return MODE_META[mode] || FALLBACK_MODE;
  }

  /* --- Small shared helpers ---------------------------------------------- */

  /* Show a message in a card's inline result panel, in the right tone.
     Used as well as a toast, not instead of one: a toast disappears, and the
     reason a connection failed is worth keeping on the screen. */
  function showResult(panel, message, tone) {
    if (!panel) { return; }
    panel.className = 'alert alert--' + (tone || 'info');
    panel.hidden = false;
    panel.textContent = message;
  }

  function hideResult(panel) {
    if (panel) { panel.hidden = true; }
  }

  /* The toast for a change that is now waiting for a person, with a link to
     the queue where it is waiting. Built with textContent and appendChild
     rather than innerHTML so a server message can never inject markup. */
  function queuedToast(message, actionsUrl) {
    App.toast(message, 'warning', 9000);
    if (!actionsUrl) { return; }

    var region = document.getElementById('toastRegion');
    var last = region ? region.lastElementChild : null;
    var body = last ? last.querySelector('.u-grow') : null;
    if (!body) { return; }

    var link = document.createElement('a');
    link.href = actionsUrl;
    link.textContent = 'Open the approval queue';
    link.className = 'toast__link';
    body.appendChild(document.createElement('br'));
    body.appendChild(link);
  }

  /* Repaint one card from a server payload. Every integration endpoint
     answers with the same shape, so this is the only place that knows how a
     card is drawn. */
  function paintCard(card, payload) {
    if (!card || !payload) { return; }

    var pill = card.querySelector('[data-status-pill]');
    if (pill && payload.status_display) {
      pill.className = 'badge badge--' + (payload.status_tone || 'neutral');
      pill.innerHTML = '<span class="badge__dot" aria-hidden="true"></span> ';
      pill.appendChild(document.createTextNode(payload.status_display));
    }

    var meta = modeMeta(payload.effective_mode);
    var block = card.querySelector('[data-mode-block]');
    if (block) {
      block.className = 'mode-pill mode-pill--' + (payload.effective_mode || 'disabled');
    }
    var label = card.querySelector('[data-mode-label]');
    if (label) {
      label.innerHTML = '<i class="fa-solid ' + meta.icon + '" aria-hidden="true"></i> ';
      label.appendChild(document.createTextNode(meta.label));
    }
    var text = card.querySelector('[data-mode-text]');
    if (text) { text.textContent = meta.explanation; }

    var modeDisplay = card.querySelector('[data-mode-display]');
    if (modeDisplay && payload.mode_display) {
      modeDisplay.textContent = payload.mode_display;
    }

    var live = card.querySelector('[data-live-calls]');
    if (live && typeof payload.live_call_count === 'number') {
      live.textContent = payload.live_call_count;
    }
    var demo = card.querySelector('[data-demo-calls]');
    if (demo && typeof payload.demo_call_count === 'number') {
      demo.textContent = payload.demo_call_count;
    }

    paintMissing(card, payload.missing);
  }

  /* The "still needed" list. Rebuilt rather than hidden, because a test that
     reveals a second missing field must not leave the first one showing on
     its own. */
  function paintMissing(card, missing) {
    var block = card.querySelector('[data-missing-block]');
    var list = card.querySelector('[data-missing-items]');
    if (!block || !list) { return; }

    if (!missing || !missing.length) {
      block.hidden = true;
      return;
    }

    list.textContent = '';
    missing.forEach(function (label) {
      var item = document.createElement('li');
      item.textContent = label;
      list.appendChild(item);
    });
    block.hidden = false;
  }

  function cardFor(element) {
    return element.closest('[data-integration-card]');
  }

  /* Find the card a dialog belongs to. The dialogs live outside the grid, in
     base.html's modals block, so they cannot simply walk up the tree. */
  function cardForKey(key) {
    return document.querySelector('[data-integration-card][data-provider-key="' + key + '"]');
  }

  document.addEventListener('DOMContentLoaded', function () {

    /* --------------------------------------------------------------------
       Test one connection.

       The badge, the mode pill and the missing list are all repainted from
       the answer, so a credential pasted a moment earlier is reflected here
       without a page reload.
       -------------------------------------------------------------------- */

    App.on('click', '[data-test-integration]', function (event) {
      event.preventDefault();
      var button = this;
      var key = button.dataset.testIntegration;
      var card = cardFor(button) || cardForKey(key);
      var panel = card ? card.querySelector('[data-test-result]') : null;

      App.setLoading(button, true, 'Checking');
      showResult(panel, 'Checking the connection...', 'info');

      App.postJSON(button.dataset.testUrl, { provider_key: key })
        .then(function (payload) {
          paintCard(card, payload);
          /* The panel takes its tone from the connection status rather than
             from the mode, so it agrees with the badge beside it: a degraded
             connection is a warning, not a failure and not a success. */
          showResult(panel, payload.message,
            payload.status_tone === 'neutral' ? 'info' : payload.status_tone);
          App.toast(payload.message, payload.ok ? 'success' : 'warning');
        })
        .catch(function (error) {
          /* The server's own sentence, never a bare "error": a connector's
             account of why it could not connect is the useful part. */
          showResult(panel, error.message, 'danger');
          App.toast(error.message, 'danger');
        })
        .finally(function () {
          App.setLoading(button, false);
        });
    });

    /* --------------------------------------------------------------------
       Enable or disable one integration.

       The switch is reverted if the write did not happen, so the control
       never shows a state the database does not hold.
       -------------------------------------------------------------------- */

    App.on('change', '[data-toggle-integration]', function () {
      var input = this;
      var key = input.dataset.toggleIntegration;
      var card = cardFor(input) || cardForKey(key);
      var wanted = input.checked;

      input.disabled = true;

      App.postJSON(input.dataset.toggleUrl, { provider_key: key, enabled: wanted })
        .then(function (payload) {
          /* Whether the change landed or queued, the switch is set from what
             the database now holds. After a queued change that is the old
             value, so the switch flicks back on its own -- which is the
             honest outcome: nothing has been altered yet. */
          input.checked = payload.is_enabled;
          paintCard(card, payload);

          if (payload.status === 'pending') {
            queuedToast(payload.message, input.dataset.actionsUrl);
            return;
          }
          App.toast(payload.message, payload.is_enabled ? 'success' : 'info');
        })
        .catch(function (error) {
          input.checked = !wanted;
          App.toast(error.message, 'danger');
        })
        .finally(function () {
          input.disabled = false;
        });
    });

    /* --------------------------------------------------------------------
       Save one integration's configuration.

       Collected from the data-config-field attributes rather than from a
       normal form submission, because the request is JSON and because a
       blank credential box must be omitted rather than sent as an empty
       string -- an empty string would mean "clear it".
       -------------------------------------------------------------------- */

    App.on('submit', '[data-config-form]', function (event) {
      event.preventDefault();

      var form = this;
      var key = form.dataset.providerKey;
      var name = form.dataset.integrationName || 'The integration';
      var button = form.querySelector('[data-config-submit]');
      var panel = form.querySelector('[data-config-result]');
      var card = cardForKey(key);

      var settings = {};
      App.qsa('[data-config-field]', form).forEach(function (field) {
        var fieldKey = field.dataset.configField;
        if (field.type === 'checkbox') {
          settings[fieldKey] = field.checked;
          return;
        }
        if (field.type === 'password' && field.value === '') {
          return;   // blank means "keep the stored value"
        }
        settings[fieldKey] = field.value;
      });

      var cleared = App.qsa('[data-clear-secret]', form)
        .filter(function (box) { return box.checked; })
        .map(function (box) { return box.dataset.clearSecret; });

      var modeSelect = form.querySelector('[data-config-mode]');

      hideResult(panel);
      App.setLoading(button, true, 'Saving');

      App.postJSON(form.dataset.configureUrl, {
        provider_key: key,
        settings: settings,
        clear_secrets: cleared,
        mode: modeSelect ? modeSelect.value : null
      })
        .then(function (payload) {
          if (payload.status === 'pending') {
            /* Not saved. Waiting for a person. Say so, and leave the dialog
               open so nothing about this looks like a completed change. */
            showResult(panel, payload.message, 'warning');
            queuedToast(payload.message, form.dataset.actionsUrl);
            paintCard(card, payload);
            return;
          }

          paintCard(card, payload);
          showResult(panel, payload.message, 'success');
          App.toast(payload.message, 'success');

          /* Clear the credential boxes on success. Their stored values are
             never sent back, so leaving what was typed on the screen would
             suggest the page is showing the stored credential. */
          App.qsa('input[type="password"][data-config-field]', form)
            .forEach(function (field) { field.value = ''; });
          App.qsa('[data-clear-secret]', form)
            .forEach(function (box) { box.checked = false; });

          if (App.closeModal) {
            var dialog = form.closest('.modal');
            if (dialog) { App.closeModal(dialog.id); }
          }
        })
        .catch(function (error) {
          showResult(panel, error.message, 'danger');
          App.toast(name + ': ' + error.message, 'danger');
        })
        .finally(function () {
          App.setLoading(button, false);
        });
    });

    /* --------------------------------------------------------------------
       Save one platform setting.

       One request per setting, matching the endpoint: each of these is
       separately audited and may separately need approval.
       -------------------------------------------------------------------- */

    App.on('click', '[data-setting-save]', function (event) {
      event.preventDefault();

      var button = this;
      var row = button.closest('[data-setting-row]');
      if (!row) { return; }

      var input = row.querySelector('[data-setting-input]');
      var panel = row.querySelector('[data-setting-result]');
      var label = row.dataset.settingLabel || 'The setting';

      if (!input) {
        App.toast(label + ' has no editable control on this page.', 'danger');
        return;
      }

      var value = input.type === 'checkbox' ? input.checked : input.value;

      if (input.type === 'password' && value === '') {
        showResult(panel,
          'Nothing was sent: the box is blank, and a blank box means "keep the ' +
          'stored value" because the stored value is never sent to the browser.',
          'info');
        return;
      }

      hideResult(panel);
      App.setLoading(button, true, 'Saving');

      App.postJSON(row.dataset.updateUrl, { key: row.dataset.settingKey, value: value })
        .then(function (payload) {
          if (payload.status === 'pending') {
            showResult(panel, payload.message, 'warning');
            queuedToast(payload.message, row.dataset.actionsUrl);
            return;
          }

          showResult(panel, payload.message, 'success');
          App.toast(payload.message, 'success');

          if (input.type === 'password') {
            input.value = '';
            input.placeholder = 'Leave blank to keep the stored value';
          }

          /* Keep the switch's own wording in step with what was just saved. */
          var switchLabel = row.querySelector('.switch__label');
          if (switchLabel && input.type === 'checkbox') {
            switchLabel.textContent = input.checked ? 'Yes' : 'No';
          }
        })
        .catch(function (error) {
          showResult(panel, error.message, 'danger');
          App.toast(label + ': ' + error.message, 'danger');
        })
        .finally(function () {
          App.setLoading(button, false);
        });
    });
  });
})(window.App);
