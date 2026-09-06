/* ==========================================================================
   config.js -- the Configurations page.

   Three behaviours:

     1. THE CASCADING DROPDOWN. Choosing a provider repopulates the model
        <select> with that provider's models. The full map is rendered into
        the page by {{ provider_model_map|json_script }}, so the first change
        needs no network round trip; /api/providers/models/ is the fallback
        for anything not in the map.
     2. TEST CONNECTION, which calls the provider's real endpoint through
        urllib on the server and reports what came back.
     3. REVEALING A STORED KEY, which only toggles the input type; the actual
        key is never sent to the browser in full.
   ========================================================================== */

(function (App) {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var providerSelect = document.getElementById('providerSelect');
    var modelSelect = document.getElementById('modelSelect');
    var modelHint = document.getElementById('modelHint');
    var providerMap = App.readJSON('providerModels') || {};

    /* --- 1. Cascading provider -> model ---------------------------------- */

    function renderModels(models) {
      if (!modelSelect) { return; }
      modelSelect.innerHTML = '';

      if (!models || !models.length) {
        modelSelect.appendChild(new Option('No models available for this provider', ''));
        modelSelect.disabled = true;
        if (modelHint) { modelHint.textContent = 'Use "Refresh models" to fetch the catalogue.'; }
        return;
      }

      modelSelect.disabled = false;
      modelSelect.appendChild(new Option('Select a model', ''));
      models.forEach(function (model) {
        var option = new Option(model.label, model.id);
        option.dataset.modelId = model.model_id;
        modelSelect.appendChild(option);
      });
      if (modelHint) {
        modelHint.textContent = models.length + ' models available.';
      }
    }

    if (providerSelect && modelSelect) {
      providerSelect.addEventListener('change', function () {
        var providerId = providerSelect.value;

        if (!providerId) {
          renderModels([]);
          return;
        }

        // Fast path: the map was rendered with the page.
        if (providerMap[providerId]) {
          renderModels(providerMap[providerId]);
          return;
        }

        // Fallback: ask the server.
        modelSelect.disabled = true;
        modelSelect.innerHTML = '';
        modelSelect.appendChild(new Option('Loading...', ''));

        App.postJSON(providerSelect.dataset.modelsUrl, { provider_id: providerId })
          .then(function (result) {
            providerMap[providerId] = result.models;
            renderModels(result.models);
          })
          .catch(function (error) {
            renderModels([]);
            App.toast(error.message, 'danger');
          });
      });

      // Populate on first load if a provider is already chosen.
      if (providerSelect.value) {
        providerSelect.dispatchEvent(new Event('change'));
      }
    }

    /* --- 2. Test connection ---------------------------------------------- */

    App.on('click', '[data-test-provider]', function (event) {
      event.preventDefault();
      var button = this;
      var card = button.closest('[data-provider-card]');
      var result = card ? card.querySelector('[data-test-result]') : null;
      var pill = card ? card.querySelector('[data-status-pill]') : null;

      App.setLoading(button, true, 'Testing');
      if (result) {
        result.className = 'alert alert--info';
        result.hidden = false;
        result.textContent = 'Contacting the provider...';
      }

      App.postJSON(button.dataset.testUrl, { provider_id: button.dataset.testProvider }, 20000)
        .then(function (payload) {
          var ok = payload.status === 'success';
          if (result) {
            result.className = 'alert alert--' + (ok ? 'success' : 'danger');
            result.textContent = payload.message +
              (payload.latency_ms ? ' (' + payload.latency_ms + ' ms)' : '');
          }
          if (pill) {
            pill.className = 'badge badge--' + (ok ? 'success' : 'danger');
            pill.textContent = payload.status_display;
          }
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

    /* --- Refresh the model catalogue ------------------------------------- */

    App.on('click', '[data-fetch-models]', function (event) {
      event.preventDefault();
      var button = this;
      var card = button.closest('[data-provider-card]');
      var result = card ? card.querySelector('[data-test-result]') : null;

      App.setLoading(button, true, 'Fetching');

      App.postJSON(button.dataset.fetchUrl, { provider_id: button.dataset.fetchModels }, 20000)
        .then(function (payload) {
          providerMap[button.dataset.fetchModels] = payload.models;
          if (result) {
            // A fallback result is a warning, not an error: the catalogue is
            // still populated, just not from the live endpoint.
            result.className = 'alert alert--' +
              (payload.source === 'live' ? 'success' : 'warning');
            result.hidden = false;
            result.textContent = payload.message;
          }
          App.setLoading(button, false);
          window.setTimeout(function () { window.location.reload(); }, 1600);
        })
        .catch(function (error) {
          if (result) {
            result.className = 'alert alert--danger';
            result.hidden = false;
            result.textContent = error.message;
          }
          App.setLoading(button, false);
        });
    });

    /* --- Email delivery ---------------------------------------------------
       The credentials live in the environment, so there is nothing to save
       here: only a connection test and a test message. */

    function showEmailResult(tone, text) {
      var box = document.querySelector('[data-email-result]');
      if (!box) { return; }
      box.className = 'alert alert--' + tone + ' u-mt-4';
      box.hidden = false;
      box.textContent = text;
    }

    App.on('click', '[data-test-email]', function (event) {
      event.preventDefault();
      var button = this;
      App.setLoading(button, true, 'Testing');
      showEmailResult('info', 'Signing in to the mail server...');

      App.postJSON(button.dataset.testUrl, {}, 30000)
        .then(function (result) {
          showEmailResult(result.ok ? 'success' : 'warning', result.message);
          var pill = document.querySelector('[data-email-status]');
          if (pill && result.configured) {
            pill.className = 'badge badge--' + (result.ok ? 'success' : 'danger');
            pill.textContent = result.ok ? 'Live' : 'Failing';
          }
          App.setLoading(button, false);
        })
        .catch(function (error) {
          showEmailResult('danger', error.message);
          App.setLoading(button, false);
        });
    });

    App.on('click', '[data-send-test-email]', function (event) {
      event.preventDefault();
      var button = this;
      var field = document.getElementById('testEmailRecipient');
      var recipient = field ? field.value.trim() : '';

      if (!recipient) {
        App.toast('Type the address to send the test to.', 'warning');
        return;
      }
      // Sending is not undoable, so it is confirmed rather than fired on a
      // single stray click.
      if (!window.confirm('Send a test message to ' + recipient + '?')) { return; }

      App.setLoading(button, true, 'Sending');
      showEmailResult('info', 'Sending...');

      App.postJSON(button.dataset.sendUrl, { recipient: recipient }, 40000)
        .then(function (result) {
          showEmailResult('success', result.message);
          App.toast(result.message, 'success');
          App.setLoading(button, false);
        })
        .catch(function (error) {
          showEmailResult('danger', error.message);
          App.setLoading(button, false);
        });
    });

    /* --- 3. Reveal a key field ------------------------------------------- */

    App.on('click', '[data-reveal-target]', function (event) {
      event.preventDefault();
      var input = document.getElementById(this.dataset.revealTarget);
      if (!input) { return; }
      var hidden = input.type === 'password';
      input.type = hidden ? 'text' : 'password';
      this.setAttribute('aria-label', hidden ? 'Hide the key' : 'Show the key');
      this.innerHTML = hidden
        ? '<i class="fa-solid fa-eye-slash" aria-hidden="true"></i>'
        : '<i class="fa-solid fa-eye" aria-hidden="true"></i>';
    });
  });
})(window.App);
