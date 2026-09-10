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
    var toggle = document.getElementById('sidebarToggle');
    var body = document.body;
    var lastFocused = null;
    var wide = window.matchMedia('(min-width: 1024px)');

    /* --- Mobile drawer --------------------------------------------------- */

    function openDrawer() {
      lastFocused = document.activeElement;
      body.classList.add('is-drawer-open');
      updateToggle();
      var first = sidebar.querySelector('a, button');
      if (first) { first.focus(); }
    }

    function closeDrawer() {
      if (!body.classList.contains('is-drawer-open')) { return; }
      body.classList.remove('is-drawer-open');
      updateToggle();
      // Prefer the navigation control. A touch tap does not always focus a button, so
      // lastFocused can be <body>, which would strand the keyboard user at the
      // top of the document instead of back where they started.
      var returnTo = (lastFocused && lastFocused !== document.body && lastFocused.focus)
        ? lastFocused
        : toggle;
      if (returnTo && returnTo.focus) { returnTo.focus(); }
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
      updateToggle();
    }

    var stored = null;
    try { stored = localStorage.getItem('sidebar_collapsed'); } catch (e) { /* ignore */ }
    applyCollapsed(stored === 'true');

    /* --- Breakpoint guard ------------------------------------------------ */

    function updateToggle() {
      if (!toggle) { return; }
      var icon = toggle.querySelector('i');
      var desktop = wide.matches;
      var drawerOpen = body.classList.contains('is-drawer-open');
      var collapsed = sidebar.classList.contains('is-collapsed');

      toggle.setAttribute('aria-expanded', desktop ? String(!collapsed) : String(drawerOpen));
      toggle.setAttribute('aria-label', desktop
        ? (collapsed ? 'Expand navigation' : 'Collapse navigation')
        : (drawerOpen ? 'Close navigation' : 'Open navigation'));

      if (icon) {
        icon.className = desktop
          ? 'fa-solid ' + (collapsed ? 'fa-chevron-right' : 'fa-chevron-left')
          : 'fa-solid ' + (drawerOpen ? 'fa-xmark' : 'fa-bars');
      }
    }

    if (toggle) {
      toggle.addEventListener('click', function () {
        if (wide.matches) {
          var next = !sidebar.classList.contains('is-collapsed');
          applyCollapsed(next);
          try { localStorage.setItem('sidebar_collapsed', String(next)); } catch (e) { /* ignore */ }
        } else if (body.classList.contains('is-drawer-open')) {
          closeDrawer();
        } else {
          openDrawer();
        }
      });
    }

    function handleBreakpoint(event) {
      if (event.matches) { closeDrawer(); }
      updateToggle();
    }
    if (wide.addEventListener) {
      wide.addEventListener('change', handleBreakpoint);
    } else if (wide.addListener) {
      wide.addListener(handleBreakpoint);   // older Safari
    }
    updateToggle();
  });
})(window.App);
