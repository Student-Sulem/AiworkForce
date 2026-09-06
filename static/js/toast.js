/* ==========================================================================
   toast.js -- transient notifications.

       App.toast('Saved', 'success');

   Django's own messages framework is rendered server-side by
   partials/_messages.html; this is for feedback that arrives from a fetch(),
   where no page reload happens and there is nothing for Django to render.
   ========================================================================== */

(function (App) {
  'use strict';

  var ICONS = {
    success: 'fa-circle-check',
    danger: 'fa-circle-exclamation',
    warning: 'fa-triangle-exclamation',
    info: 'fa-circle-info'
  };

  App.toast = function (message, type, timeoutMs) {
    var region = document.getElementById('toastRegion');
    if (!region) {
      // Nowhere to render: fall back to something the user can still see.
      window.alert(message);
      return;
    }

    type = type || 'info';

    var toast = document.createElement('div');
    toast.className = 'toast toast--' + type;
    toast.setAttribute('role', type === 'danger' ? 'alert' : 'status');
    toast.innerHTML =
      '<i class="fa-solid ' + (ICONS[type] || ICONS.info) + '" aria-hidden="true"></i>' +
      '<span class="u-grow">' + App.escapeHtml(message) + '</span>' +
      '<button class="modal__close" type="button" aria-label="Dismiss">&times;</button>';

    function dismiss() {
      toast.classList.add('is-leaving');
      window.setTimeout(function () { toast.remove(); }, 200);
    }

    toast.querySelector('button').addEventListener('click', dismiss);
    region.appendChild(toast);
    window.setTimeout(dismiss, timeoutMs || 4500);
  };
})(window.App);
