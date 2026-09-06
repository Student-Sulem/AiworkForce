/* ==========================================================================
   nav.js -- the application shell's navigation behaviour.

   Two separate things share this file because they share the sidebar element:

     * Below 768px the sidebar is an off-canvas drawer. It needs an overlay,
       Escape to close, a focus trap while open, and focus returned to the
       hamburger afterwards.
     * At 1024px and above the sidebar is permanent but collapsible, and the
       choice is remembered in localStorage.

   The matchMedia listener at the end is not optional: without it, rotating a
   phone to landscape while the drawer is open leaves the page scroll-locked
   with no visible way to unlock it.
   ========================================================================== */

(function (App) {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var sidebar = document.getElementById('appSidebar');
    if (!sidebar) { return; }

    var overlay = document.getElementById('drawerOverlay');
    var hamburger = document.getElementById('drawerToggle');
    var collapser = document.getElementById('sidebarCollapse');
    var body = document.body;
    var lastFocused = null;

    /* --- Mobile drawer --------------------------------------------------- */

    function openDrawer() {
      lastFocused = document.activeElement;
      body.classList.add('is-drawer-open');
      if (hamburger) {
        hamburger.setAttribute('aria-expanded', 'true');
        hamburger.setAttribute('aria-label', 'Close navigation');
      }
      var first = sidebar.querySelector('a, button');
      if (first) { first.focus(); }
    }

    function closeDrawer() {
      if (!body.classList.contains('is-drawer-open')) { return; }
      body.classList.remove('is-drawer-open');
      if (hamburger) {
        hamburger.setAttribute('aria-expanded', 'false');
        hamburger.setAttribute('aria-label', 'Open navigation');
      }
      // Prefer the hamburger. A touch tap does not always focus a button, so
      // lastFocused can be <body>, which would strand the keyboard user at the
      // top of the document instead of back where they started.
      var returnTo = (lastFocused && lastFocused !== document.body && lastFocused.focus)
        ? lastFocused
        : hamburger;
      if (returnTo && returnTo.focus) { returnTo.focus(); }
    }

    if (hamburger) {
      hamburger.addEventListener('click', function () {
        if (body.classList.contains('is-drawer-open')) { closeDrawer(); } else { openDrawer(); }
      });
    }

    if (overlay) { overlay.addEventListener('click', closeDrawer); }

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { closeDrawer(); }
    });

    // Following a link should close the drawer behind it.
    App.on('click', '.sidebar__link', closeDrawer, sidebar);

    // Keep Tab inside the drawer while it is open.
    sidebar.addEventListener('keydown', function (event) {
      if (event.key !== 'Tab' || !body.classList.contains('is-drawer-open')) { return; }
      var items = App.qsa('a[href], button:not([disabled])', sidebar);
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

    /* --- Desktop collapse ------------------------------------------------ */

    function applyCollapsed(isCollapsed) {
      sidebar.classList.toggle('is-collapsed', isCollapsed);
      body.classList.toggle('is-sidebar-collapsed', isCollapsed);
      if (collapser) {
        collapser.setAttribute('aria-expanded', isCollapsed ? 'false' : 'true');
        collapser.setAttribute('aria-label',
          isCollapsed ? 'Expand the sidebar' : 'Collapse the sidebar');
      }
    }

    var stored = null;
    try { stored = localStorage.getItem('sidebar_collapsed'); } catch (e) { /* ignore */ }
    applyCollapsed(stored === 'true');

    if (collapser) {
      collapser.addEventListener('click', function () {
        var next = !sidebar.classList.contains('is-collapsed');
        applyCollapsed(next);
        try { localStorage.setItem('sidebar_collapsed', String(next)); } catch (e) { /* ignore */ }
      });
    }

    /* --- Breakpoint guard ------------------------------------------------ */

    var wide = window.matchMedia('(min-width: 768px)');
    function handleBreakpoint(event) {
      if (event.matches) { closeDrawer(); }
    }
    if (wide.addEventListener) {
      wide.addEventListener('change', handleBreakpoint);
    } else if (wide.addListener) {
      wide.addListener(handleBreakpoint);   // older Safari
    }
  });
})(window.App);
