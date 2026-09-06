/* ==========================================================================
   landing.js -- the public marketing page.

   Two small behaviours. The FAQ accordion needs no JavaScript at all: it uses
   native <details> and <summary>, with the plus/minus glyph swapped by the
   [open] attribute selector in pages.css.
   ========================================================================== */

(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {

    /* --- Counting animation on the statistics strip ---------------------- */

    var stats = Array.prototype.slice.call(document.querySelectorAll('.stat__value'));
    var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (stats.length && !reduceMotion && 'IntersectionObserver' in window) {
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting || entry.target.dataset.counted) { return; }

          var element = entry.target;
          element.dataset.counted = 'true';

          var text = element.textContent.trim();
          var target = parseInt(text.replace(/\D/g, ''), 10);
          if (isNaN(target) || target === 0) { return; }

          var suffix = text.replace(/[\d]/g, '');
          var current = 0;
          var step = Math.max(1, Math.ceil(target / 24));

          var timer = setInterval(function () {
            current += step;
            if (current >= target) {
              current = target;
              clearInterval(timer);
            }
            element.textContent = current + suffix;
          }, 30);
        });
      }, { threshold: 0.4 });

      stats.forEach(function (stat) { observer.observe(stat); });
    }

    /* --- ROI calculator --------------------------------------------------- */

    var slider = document.getElementById('roiHours');
    if (!slider) { return; }

    var hoursLabel = document.getElementById('roiHoursValue');
    var hoursSaved = document.getElementById('roiHoursSaved');
    var daysSaved = document.getElementById('roiDaysSaved');
    var reviewTime = document.getElementById('roiReviewTime');

    var AUTOMATION_RATE = 0.6;   // share of repeatable work an employee can draft
    var REVIEW_RATE = 0.1;       // share of that time spent reviewing the output

    function recalculate() {
      var hours = parseInt(slider.value, 10);
      var saved = Math.round(hours * AUTOMATION_RATE);
      var review = Math.max(1, Math.round(hours * REVIEW_RATE));

      if (hoursLabel) { hoursLabel.textContent = hours; }
      if (hoursSaved) { hoursSaved.textContent = saved; }
      if (daysSaved) { daysSaved.textContent = Math.round(saved * 52 / 8); }
      if (reviewTime) { reviewTime.textContent = review; }
    }

    slider.addEventListener('input', recalculate);
    recalculate();
  });
})();
