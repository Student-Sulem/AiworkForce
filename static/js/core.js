/* ==========================================================================
   core.js -- shared helpers on a single global, window.App.

   Loaded first on every page. Everything else assumes App exists.
   No framework, no build step, no dependencies.
   ========================================================================== */

window.App = window.App || {};

(function (App) {
  'use strict';

  /* --- Selection -------------------------------------------------------- */

  App.qs = function (selector, root) {
    return (root || document).querySelector(selector);
  };

  App.qsa = function (selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  };

  /* Event delegation, so handlers survive DOM that is added later.
     Usage: App.on('click', '[data-modal-open]', function (event) { ... }); */
  App.on = function (type, selector, handler, root) {
    (root || document).addEventListener(type, function (event) {
      var target = event.target.closest(selector);
      if (target) { handler.call(target, event, target); }
    });
  };

  App.debounce = function (fn, wait) {
    var timer;
    return function () {
      var args = arguments;
      var context = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(context, args); }, wait || 200);
    };
  };

  /* --- CSRF ------------------------------------------------------------- */

  App.getCookie = function (name) {
    if (!document.cookie) { return null; }
    var parts = document.cookie.split('; ');
    for (var i = 0; i < parts.length; i++) {
      var pair = parts[i].split('=');
      if (decodeURIComponent(pair[0]) === name) {
        return decodeURIComponent(pair.slice(1).join('='));
      }
    }
    return null;
  };

  /* Prefers the hidden {% csrf_token %} input that base.html always renders,
     and falls back to the cookie. Django rejects a POST without this. */
  App.csrfToken = function () {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return (input && input.value) || App.getCookie('csrftoken') || '';
  };

  /* --- Networking -------------------------------------------------------- */

  /* POST JSON with the CSRF header, a timeout, and consistent errors.
     Always resolves or rejects; never leaves a button spinning forever. */
  App.postJSON = function (url, data, timeoutMs) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs || 15000);

    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': App.csrfToken(),
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify(data || {}),
      signal: controller.signal
    }).then(function (response) {
      return response.json()
        .catch(function () { throw new Error('The server returned an unreadable response.'); })
        .then(function (payload) {
          if (!response.ok) {
            throw new Error(payload.message || ('The server returned ' + response.status + '.'));
          }
          return payload;
        });
    }).catch(function (error) {
      if (error.name === 'AbortError') {
        throw new Error('The request timed out. Please try again.');
      }
      throw error;
    }).finally(function () {
      clearTimeout(timer);
    });
  };

  /* --- Button loading state ---------------------------------------------- */

  App.setLoading = function (button, isLoading, loadingText) {
    if (!button) { return; }
    if (isLoading) {
      button.dataset.originalHtml = button.innerHTML;
      button.disabled = true;
      button.classList.add('is-loading');
      button.innerHTML = '<i class="fa-solid fa-circle-notch"></i> ' +
        (loadingText || 'Working');
    } else {
      button.disabled = false;
      button.classList.remove('is-loading');
      if (button.dataset.originalHtml) {
        button.innerHTML = button.dataset.originalHtml;
        delete button.dataset.originalHtml;
      }
    }
  };

  /* --- Misc -------------------------------------------------------------- */

  App.escapeHtml = function (value) {
    var div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  };

  /* Read a <script type="application/json"> block written by {{ x|json_script }}. */
  App.readJSON = function (elementId) {
    var element = document.getElementById(elementId);
    if (!element) { return null; }
    try { return JSON.parse(element.textContent); } catch (e) { return null; }
  };
})(window.App);
