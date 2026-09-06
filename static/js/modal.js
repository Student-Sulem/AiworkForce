/* ==========================================================================
   modal.js -- a small dialog system driven entirely by data attributes.

       <button data-modal-open="newCampaignModal">New campaign</button>
       <div class="modal" id="newCampaignModal"> ... </div>
       <button data-modal-close>Cancel</button>

   Delegation is used throughout, so a modal added to the page later still
   works without re-binding anything.
   ========================================================================== */

(function (App) {
  'use strict';

  var lastFocused = null;

  function openModal(id) {
    var modal = document.getElementById(id);
    if (!modal) { return; }
    lastFocused = document.activeElement;
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';

    var focusable = modal.querySelector(
      'input:not([type="hidden"]), textarea, select, button, a[href]');
    if (focusable) { focusable.focus(); }
  }

  function closeModal(id) {
    var modal = id ? document.getElementById(id) : document.querySelector('.modal.is-open');
    if (!modal) { return; }
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    if (!document.querySelector('.modal.is-open')) {
      document.body.style.overflow = '';
    }
    if (lastFocused && lastFocused.focus) { lastFocused.focus(); }
  }

  App.openModal = openModal;
  App.closeModal = closeModal;

  document.addEventListener('DOMContentLoaded', function () {
    App.on('click', '[data-modal-open]', function (event) {
      event.preventDefault();
      openModal(this.dataset.modalOpen);
    });

    App.on('click', '[data-modal-close]', function (event) {
      event.preventDefault();
      closeModal(this.dataset.modalClose || null);
    });

    // A click on the backdrop, but not inside the box, dismisses the dialog.
    App.on('click', '.modal', function (event) {
      if (event.target === this) { closeModal(this.id); }
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { closeModal(); }
    });

    // Keep Tab inside an open dialog.
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Tab') { return; }
      var modal = document.querySelector('.modal.is-open');
      if (!modal) { return; }
      var items = App.qsa(
        'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), ' +
        'select:not([disabled]), textarea:not([disabled])', modal);
      if (!items.length) { return; }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    // A modal whose form failed server-side validation reopens itself.
    var autoOpen = document.querySelector('[data-modal-autoopen]');
    if (autoOpen) { openModal(autoOpen.getAttribute('data-modal-autoopen')); }
  });
})(window.App);
