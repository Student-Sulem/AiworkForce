/* ==========================================================================
   theme.js -- the light/dark toggle.

   The theme is *applied* by a tiny inline script in partials/_head.html,
   which runs before the stylesheets so the page never flashes the wrong
   colours. This file only handles the button: flipping the attribute,
   saving the choice, and keeping the accessible label in step.

   Storage is twofold. localStorage makes the choice instant on the next
   page load, and a background POST to /api/preferences/theme/ persists it
   on the user's profile so it follows them to another device.
   ========================================================================== */

(function (App) {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var button = document.getElementById('themeToggle');

    // Remove the preload guard so colour transitions work from now on.
    window.setTimeout(function () {
      document.documentElement.classList.remove('is-preload');
    }, 60);

    if (!button) { return; }

    function currentTheme() {
      return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }

    function sync() {
      var isDark = currentTheme() === 'dark';
      button.setAttribute('aria-pressed', isDark ? 'true' : 'false');
      button.setAttribute('aria-label',
        isDark ? 'Switch to the light theme' : 'Switch to the dark theme');
      button.innerHTML = isDark
        ? '<i class="fa-solid fa-sun" aria-hidden="true"></i>'
        : '<i class="fa-solid fa-moon" aria-hidden="true"></i>';
    }

    button.addEventListener('click', function () {
      var next = currentTheme() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);

      try {
        localStorage.setItem('theme', next);
      } catch (e) {
        /* Private browsing can refuse storage. The attribute still applied. */
      }

      sync();

      // Best effort. A failure here changes nothing the user can see.
      if (button.dataset.persistUrl) {
        App.postJSON(button.dataset.persistUrl, { theme: next }).catch(function () {});
      }
    });

    sync();
  });
})(window.App);
