// The diff viewer of diffs.html, inlined into the page. review.py worked out every
// number; this only draws them, from the JSON in #data. Diff text goes in with
// textContent, never as markup.

(function () {
  'use strict';
  var data = JSON.parse(document.getElementById('data').textContent);
  var viewer = document.getElementById('viewer');
  var FULL = 'diff-full.patch';
  // The most cells the line diff of two Trials' files may fill (16 MB); past it,
  // the page points at `build-bench diff` instead of freezing.
  var LIMIT = 4000000;
  var shown = null;  // The Trial and file on show, to tell a new view from a changed control.

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function readHash() {
    var q = new URLSearchParams(location.hash.slice(1));
    return {
      trial: q.get('trial'), file: q.get('file'), pin: q.get('pin'),
      vs: q.get('vs'), all: q.get('all') === '1'
    };
  }

  function hashOf(v) {
    var parts = ['trial=' + encodeURIComponent(v.trial),
      'file=' + encodeURIComponent(v.file).replace(/%2F/g, '/')];
    if (v.vs) parts.push('vs=' + encodeURIComponent(v.vs));
    if (v.all) parts.push('all=1');
    if (v.pin !== null && v.pin !== undefined) parts.push('pin=' + v.pin);
    return '#' + parts.join('&');
  }

  function go(v) { location.hash = hashOf(v); }

  // --- Pins -------------------------------------------------------------------

  function pinCard(p) {
    var card = el('div', 'pin sev-' + p.severity);
    card.id = 'pin-' + p.id;
    card.tabIndex = -1;
    var head = el('p', 'pin-head');
    head.appendChild(el('span', 'sev sev-' + p.severity, p.severity));
    var about = ' ' + p.kind + ' by ' + p.judge + ' · ' + (p.category || 'no category');
    if (p.line !== null) about += ' · line ' + p.line + (p.end !== null ? '–' + p.end : '');
    head.appendChild(document.createTextNode(about));
    if (p.defect) {
      head.appendChild(document.createTextNode(' · '));
      var a = el('a', null, 'Defect ' + p.defect);
      a.href = '#defect-' + encodeURIComponent(p.defect);
      head.appendChild(a);
    }
    card.appendChild(head);
    card.appendChild(el('p', 'pin-title', p.title));
    if (p.detail) card.appendChild(el('p', 'pin-detail', p.detail));
    card.appendChild(el('p', 'pin-where', 'Where: ' + (p.where || 'not given')));
    return card;
  }

  // --- The diff table -----------------------------------------------------------

  // The runs of rows to show: each change with `context` rows either side.
  function runsAround(rows, context) {
    var near = [];
    rows.forEach(function (r, i) {
      if (r[0] === ' ') return;
      for (var k = Math.max(0, i - context); k <= Math.min(rows.length - 1, i + context); k++) {
        near[k] = true;
      }
    });
    // A line that would be hidden alone is shown: that is shorter than saying so.
    var lone = [];
    for (var g = 0; g < rows.length; g++) {
      lone[g] = !near[g] && (g === 0 || near[g - 1]) && (g === rows.length - 1 || near[g + 1]);
    }
    var runs = [];
    for (var i = 0; i < rows.length; i++) {
      if (!near[i] && !lone[i]) continue;
      var last = runs[runs.length - 1];
      if (last && last[1] === i) last[1] = i + 1; else runs.push([i, i + 1]);
    }
    return runs;
  }

  function gapRow(text, expand) {
    var tr = el('tr', 'gap');
    var td = el('td');
    td.colSpan = 4;
    if (expand) {
      var b = el('button', null, text);
      b.type = 'button';
      b.addEventListener('click', expand);
      td.appendChild(b);
    } else {
      td.textContent = text;
    }
    tr.appendChild(td);
    return tr;
  }

  function lineRow(r) {
    var tr = el('tr', r[0] === '+' ? 'add' : r[0] === '-' ? 'del' : 'ctx');
    tr.appendChild(el('td', 'num', r[1]));
    tr.appendChild(el('td', 'num', r[2]));
    tr.appendChild(el('td', 'mark', r[0] === ' ' ? '' : r[0] === '-' ? '−' : '+'));
    tr.appendChild(el('td', 'code', r[3]));
    return tr;
  }

  // rows: [mark, old number, new number, text]; runs: [start, end) of rows to show;
  // starts: where the saved diff's hunks start; pinsAt: row index to pins under it;
  // expand: what a hidden-lines row does when pressed, if anything.
  function diffTable(rows, runs, starts, pinsAt, expand, label) {
    var table = el('table', 'diff');
    table.setAttribute('aria-label', label);
    var body = el('tbody');
    var at = 0;
    runs.forEach(function (run) {
      if (run[0] > at) {
        var n = run[0] - at;
        body.appendChild(gapRow(n + ' unchanged line' + (n === 1 ? '' : 's') + ' hidden', expand));
      } else if (run[0] > 0 && starts.indexOf(run[0]) >= 0) {
        body.appendChild(gapRow('Lines not on this page', null));
      }
      for (var i = run[0]; i < run[1]; i++) {
        body.appendChild(lineRow(rows[i]));
        (pinsAt[i] || []).forEach(function (p) {
          var tr = el('tr', 'pin-row');
          var td = el('td');
          td.colSpan = 4;
          td.appendChild(pinCard(p));
          tr.appendChild(td);
          body.appendChild(tr);
        });
      }
      at = run[1];
    });
    if (at < rows.length) {
      var left = rows.length - at;
      body.appendChild(gapRow(left + ' unchanged line' + (left === 1 ? '' : 's') + ' hidden', expand));
    }
    table.appendChild(body);
    return table;
  }

  // --- Two Trials' final versions --------------------------------------------------

  // Every row of a line diff of a against b, or null when it is too large to work out here.
  function lineDiff(a, b) {
    var start = 0;
    while (start < a.length && start < b.length && a[start] === b[start]) start++;
    var endA = a.length, endB = b.length;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
    var n = endA - start, m = endB - start;
    if ((n + 1) * (m + 1) > LIMIT) return null;
    var w = m + 1, t = new Uint32Array((n + 1) * w);
    for (var i = n - 1; i >= 0; i--) {
      for (var j = m - 1; j >= 0; j--) {
        t[i * w + j] = a[start + i] === b[start + j] ? t[(i + 1) * w + j + 1] + 1
          : Math.max(t[(i + 1) * w + j], t[i * w + j + 1]);
      }
    }
    var rows = [], x = 0, y = 0;
    function same() { rows.push([' ', x + 1, y + 1, a[x]]); x++; y++; }
    for (var k = 0; k < start; k++) same();
    i = 0; j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && a[start + i] === b[start + j]) { same(); i++; j++; }
      else if (j >= m || (i < n && t[(i + 1) * w + j] >= t[i * w + j + 1])) {
        rows.push(['-', x + 1, null, a[x]]); x++; i++;
      } else {
        rows.push(['+', null, y + 1, b[y]]); y++; j++;
      }
    }
    while (x < a.length) same();
    return rows;
  }

  // A Trial's final version of a file, or the base version when it did not change it.
  function version(trial, file, other) {
    var e = data.trials[trial].files[file];
    if (e) {
      if (e.status === 'deleted') return { lines: [], note: trial + ' removed this file.' };
      return { lines: e.lines.filter(function (r) { return r[0] !== '-'; })
        .map(function (r) { return r[3]; }) };
    }
    var base = other.lines.filter(function (r) { return r[0] !== '+'; })
      .map(function (r) { return r[3]; });
    if (other.status === 'added' || other.status === 'renamed') return { lines: [], note: trial + ' does not have this file.' };
    return { lines: base, note: trial + ' did not change this file: it has the base version.' };
  }

  function comparable(trial, file, entry) {
    var mine = data.trials[trial];
    if (mine.source !== FULL || !entry.whole) return [];
    return Object.keys(data.trials).filter(function (other) {
      var t = data.trials[other];
      if (other === trial || t.state !== 'built' || t.source !== FULL) return false;
      return !t.files[file] || t.files[file].whole;
    });
  }

  // --- The viewer -------------------------------------------------------------------

  function controls(view, entry) {
    var bar = el('div', 'viewer-head');
    var h = el('h2', null, view.file);
    h.id = 'viewer-title';
    h.tabIndex = -1;
    bar.appendChild(h);
    if (entry.whole) {
      var b = el('button', 'control', view.all ? 'Show changes only' : 'Show the whole file');
      b.type = 'button';
      b.setAttribute('aria-pressed', view.all ? 'true' : 'false');
      b.addEventListener('click', function () {
        go({ trial: view.trial, file: view.file, vs: view.vs, all: !view.all, pin: null });
      });
      bar.appendChild(b);
    }
    var others = comparable(view.trial, view.file, entry);
    if (others.length) {
      var label = el('label', null, 'Compare with ');
      var select = el('select', 'control');
      var none = el('option', null, 'no other Trial');
      none.value = '';
      select.appendChild(none);
      others.forEach(function (o) {
        var opt = el('option', null, o + (data.trials[o].files[view.file] ? '' : ' (unchanged)'));
        opt.value = o;
        opt.selected = o === view.vs;
        select.appendChild(opt);
      });
      select.addEventListener('change', function () {
        go({ trial: view.trial, file: view.file, vs: select.value || null, all: view.all, pin: null });
      });
      label.appendChild(select);
      bar.appendChild(label);
    }
    return bar;
  }

  function describe(view, entry) {
    var t = data.trials[view.trial];
    var what = view.trial + ' (' + t.variant + ')';
    if (entry.status === 'added') what += ' added this file.';
    else if (entry.status === 'deleted') what += ' removed this file.';
    else if (entry.status === 'renamed') what += ' renamed it from ' + entry.old_path + '.';
    else what += ' changed this file.';
    what += ' +' + entry.added + ' −' + entry.removed + '.';
    if (t.source !== FULL) {
      what += ' Only the plain diff was saved for this Trial, so only the lines around each change are here.';
    }
    if (entry.trimmed) {
      what += ' An ignored file, so only the lines around each change are on this page.';
    }
    return el('p', 'viewer-note', what);
  }

  function showDiff(view, entry) {
    var pins = entry.pins.map(function (i) { return data.pins[i]; });
    var top = pins.filter(function (p) { return p.at === null; });
    if (top.length) {
      var list = el('div', 'pins');
      top.forEach(function (p) { list.appendChild(pinCard(p)); });
      viewer.appendChild(list);
    }
    if (entry.binary) {
      viewer.appendChild(el('p', 'viewer-note', 'A binary file: its content is not in the diff.'));
      return;
    }
    var pinsAt = {};
    pins.forEach(function (p) { if (p.at !== null) (pinsAt[p.at] = pinsAt[p.at] || []).push(p); });
    var all = view.all && entry.whole;
    var runs = all ? [[0, entry.lines.length]] : entry.shown;
    var expand = entry.whole ? function () {
      go({ trial: view.trial, file: view.file, vs: null, all: true, pin: null });
    } : null;
    viewer.appendChild(diffTable(entry.lines, runs, entry.starts, pinsAt, expand,
      view.trial + "'s change to " + view.file + ': line before, line after, change, code'));
  }

  function showCompare(view, entry) {
    var a = version(view.trial, view.file, entry), b = version(view.vs, view.file, entry);
    viewer.appendChild(el('p', 'viewer-note', 'The final version in ' + view.trial +
      ' (−) against the one in ' + view.vs + ' (+). ' + (b.note || '')));
    var rows = lineDiff(a.lines, b.lines);
    if (rows === null) {
      viewer.appendChild(el('p', 'viewer-note', 'Too large to compare here. Run: build-bench diff ' +
        view.trial + ' ' + view.vs + ' ' + view.file));
      return;
    }
    if (!rows.some(function (r) { return r[0] !== ' '; })) {
      viewer.appendChild(el('p', 'viewer-note', 'The two final versions are the same.'));
      return;
    }
    var runs = view.all ? [[0, rows.length]] : runsAround(rows, data.context);
    var expand = function () {
      go({ trial: view.trial, file: view.file, vs: view.vs, all: true, pin: null });
    };
    viewer.appendChild(diffTable(rows, runs, [], {}, expand,
      view.trial + ' against ' + view.vs + ': line in each, change, code'));
  }

  function markCell(view) {
    document.querySelectorAll('a.cell').forEach(function (a) {
      var on = view && a.getAttribute('data-trial') === view.trial &&
        a.getAttribute('data-file') === view.file;
      if (on) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
    });
  }

  function render() {
    var view = readHash();
    if (location.hash.indexOf('#defect-') === 0) {
      var d = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (d) d.open = true;
    }
    if (!view.trial || view.file === null) return;
    var t = data.trials[view.trial];
    var entry = t && t.files[view.file];
    viewer.replaceChildren();
    markCell(view);
    if (!entry) {
      viewer.appendChild(el('p', 'muted', 'That Trial did not change ' + view.file + '.'));
      return;
    }
    var ignored = document.getElementById('ignored-rows');
    if (ignored && ignored.querySelector('a.cell[aria-current]')) setIgnored(true);
    if (view.vs && comparable(view.trial, view.file, entry).indexOf(view.vs) < 0) view.vs = null;
    viewer.appendChild(controls(view, entry));
    viewer.appendChild(describe(view, entry));
    if (view.vs) showCompare(view, entry); else showDiff(view, entry);
    var key = view.trial + '\n' + view.file;
    var target = view.pin !== null ? document.getElementById('pin-' + view.pin) : null;
    if (target) {
      target.classList.add('target');
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: 'center' });
    } else if (key !== shown) {
      document.getElementById('viewer-title').focus({ preventScroll: true });
      viewer.scrollIntoView({ block: 'start' });
    }
    shown = key;
  }

  function setIgnored(open) {
    var rows = document.getElementById('ignored-rows');
    var button = document.getElementById('toggle-ignored');
    if (!rows || !button) return;
    rows.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  var toggle = document.getElementById('toggle-ignored');
  if (toggle) {
    toggle.addEventListener('click', function () {
      setIgnored(toggle.getAttribute('aria-expanded') !== 'true');
    });
  }
  document.getElementById('theme').addEventListener('click', function () {
    var root = document.documentElement;
    var dark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') &&
       window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
  window.addEventListener('hashchange', render);
  render();
})();
