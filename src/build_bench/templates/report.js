// Tooltips for the timeline rows and the theme button, inlined into report.html.

(function () {
  var tip = document.getElementById('tip');
  function fill(el) {
    tip.replaceChildren();
    el.getAttribute('data-tip').split('\n').forEach(function (line, i) {
      var row = document.createElement('div');
      row.className = i === 0 ? 'tip-value' : 'tip-label';
      row.textContent = line;
      tip.appendChild(row);
    });
    tip.hidden = false;
  }
  function place(x, y) {
    var pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    var left = x + pad, top = y + pad;
    if (left + w > window.innerWidth - 8) left = x - w - pad;
    if (top + h > window.innerHeight - 8) top = y - h - pad;
    tip.style.left = Math.max(8, left) + 'px';
    tip.style.top = Math.max(8, top) + 'px';
  }
  function hide() { tip.hidden = true; }
  document.querySelectorAll('.agent').forEach(function (el) {
    el.addEventListener('pointerenter', function (e) { fill(el); place(e.clientX, e.clientY); });
    el.addEventListener('pointermove', function (e) { place(e.clientX, e.clientY); });
    el.addEventListener('pointerleave', hide);
    el.addEventListener('focus', function () {
      fill(el);
      var r = el.getBoundingClientRect();
      place(r.left + Math.min(r.width / 2, 120), r.bottom - 6);
    });
    el.addEventListener('blur', hide);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
  var button = document.getElementById('theme');
  button.addEventListener('click', function () {
    var root = document.documentElement;
    var dark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') &&
       window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
})();
