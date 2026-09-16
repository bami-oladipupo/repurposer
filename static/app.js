// Toasts from HX-Trigger headers, the edit drawer, calendar drag and drop, menus, tabs, slot editing, drop zone.
(function () {
  const toasts = document.getElementById('toasts');
  function toast(msg) {
    if (!msg) return;
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    toasts.appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }
  window.toast = toast;
  document.body.addEventListener('toast', e => toast(e.detail.value || e.detail));
  document.body.addEventListener('refresh', () => setTimeout(() => location.reload(), 400));
  document.body.addEventListener('htmx:responseError', e => {
    const s = e.detail.xhr.status;
    toast(s >= 500 ? 'The server hit an error. Check Runs for the full message.' : 'Request failed (' + s + ')');
  });
  document.body.addEventListener('htmx:sendError', () => toast('Server unreachable. Is the web app running?'));

  // off-canvas sidebar on phones: close on navigation or scrim tap
  document.addEventListener('click', e => {
    if (!document.body.classList.contains('nav-open')) return;
    const rail = document.getElementById('rail');
    if (e.target.closest('#rail a') || !e.target.closest('#rail, .topbar')) document.body.classList.remove('nav-open');
  });

  // kebab menus: one open at a time, close on outside click or Escape
  document.addEventListener('click', e => {
    const inMenu = e.target.closest('.menu');
    document.querySelectorAll('.menu.open').forEach(m => { if (m !== inMenu) m.classList.remove('open'); });
    if (inMenu && e.target.closest('.pop a, .pop button')) inMenu.classList.remove('open');
  });

  // drawer
  const drawer = document.getElementById('drawer');
  const panel = drawer && drawer.querySelector('.panel');
  window.closeDrawer = function () { drawer.classList.remove('open'); panel.innerHTML = ''; };
  if (drawer) {
    drawer.querySelector('.scrim').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      closeDrawer();
      document.querySelectorAll('.menu.open').forEach(m => m.classList.remove('open'));
      document.body.classList.remove('nav-open');
    });
    document.body.addEventListener('htmx:afterSwap', e => {
      if (e.detail.target === panel) drawer.classList.add('open');
    });
    document.body.addEventListener('htmx:afterRequest', e => {
      const el = e.detail.elt;
      if (!el || !el.matches || !e.detail.successful) return;
      if (el.matches('form[data-drawer-form]')) closeDrawer();
      if (el.matches('[data-drawer-action]')) { closeDrawer(); setTimeout(() => location.reload(), 250); }
    });
  }

  // settings tabs, remembered per page in the URL hash
  const tabs = document.querySelector('.tabs');
  if (tabs) {
    const show = name => {
      tabs.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('on', p.dataset.panel === name));
    };
    tabs.addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) { show(b.dataset.tab); history.replaceState(null, '', '#' + b.dataset.tab); } });
    const h = location.hash.slice(1);
    if (h && tabs.querySelector(`button[data-tab="${h}"]`)) show(h);
    // a server-side validation error should reopen the tab that holds the field
    const err = document.querySelector('.notice.danger');
    if (err && /slot|Mon|Tue|Wed|Thu|Fri|Sat|Sun|delay/.test(err.textContent)) show('publish');
  }

  // calendar drag and drop: drop an entry on a day cell to move it, time of day is kept
  let dragging = null;
  document.addEventListener('dragstart', e => {
    const ev = e.target.closest && e.target.closest('.ev[draggable="true"]');
    if (!ev) return;
    dragging = ev;
    ev.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', ev.dataset.id);
  });
  document.addEventListener('dragend', () => { if (dragging) dragging.classList.remove('dragging'); dragging = null;
    document.querySelectorAll('.cell.over').forEach(c => c.classList.remove('over')); });
  document.addEventListener('dragover', e => {
    const cell = e.target.closest && e.target.closest('.cell[data-day]');
    if (!cell || !dragging) return;
    e.preventDefault();
    cell.classList.add('over');
  });
  document.addEventListener('dragleave', e => {
    const cell = e.target.closest && e.target.closest('.cell[data-day]');
    if (cell) cell.classList.remove('over');
  });
  document.addEventListener('drop', e => {
    const cell = e.target.closest && e.target.closest('.cell[data-day]');
    if (!cell || !dragging) return;
    e.preventDefault();
    const id = dragging.dataset.id, platform = dragging.dataset.platform, day = cell.dataset.day;
    if (dragging.dataset.day === day) return;
    htmx.ajax('POST', `/videos/${id}/${platform}/reschedule`, { values: { day: day }, swap: 'none' });
  });

  // weekly slots: add a time to a day, copy Monday to weekdays, warn on slots under two hours apart
  const slots = document.querySelector('.slots');
  if (slots) {
    const MAX = 5;
    function addTime(day, value) {
      const times = day.querySelector('.times');
      const inputs = times.querySelectorAll('input[type=time]');
      if (inputs.length >= MAX) { toast('Five slots a day is the limit'); return null; }
      const inp = document.createElement('input');
      inp.type = 'time'; inp.name = day.dataset.name; inp.step = 900; inp.value = value || '';
      times.insertBefore(inp, times.querySelector('.add'));
      day.classList.remove('off');
      return inp;
    }
    slots.addEventListener('click', e => {
      const add = e.target.closest('.add');
      if (add) { e.preventDefault(); const inp = addTime(add.closest('.day')); if (inp) inp.focus(); }
    });
    slots.addEventListener('change', e => {
      const day = e.target.closest('.day');
      if (!day) return;
      const vals = [...day.querySelectorAll('input[type=time]')].map(i => i.value).filter(Boolean)
        .map(v => parseInt(v.slice(0, 2), 10) * 60 + parseInt(v.slice(3), 10)).sort((a, b) => a - b);
      for (let i = 1; i < vals.length; i++) if (vals[i] - vals[i - 1] < 120) { toast(day.dataset.label + ': slots must be at least two hours apart'); break; }
      if (!e.target.value) { e.target.remove(); if (!day.querySelector('input[type=time]')) day.classList.add('off'); }
    });
    const copy = document.getElementById('copy-monday');
    if (copy) copy.addEventListener('click', e => {
      e.preventDefault();
      const days = [...slots.querySelectorAll('.day')];
      const mon = [...days[0].querySelectorAll('input[type=time]')].map(i => i.value).filter(Boolean);
      if (!mon.length) { toast('Monday has no slots to copy'); return; }
      days.slice(1, 5).forEach(d => { d.querySelectorAll('input[type=time]').forEach(i => i.remove()); mon.forEach(v => addTime(d, v)); });
      toast('Monday copied to Tue to Fri');
    });
  }

  // template placeholder chips insert at the caret of the field they belong to
  document.querySelectorAll('.tokens').forEach(box => box.addEventListener('click', e => {
    const b = e.target.closest('button[data-token]');
    if (!b) return;
    e.preventDefault();
    const field = document.querySelector(box.dataset.for);
    if (!field) return;
    const s = field.selectionStart ?? field.value.length, t = field.selectionEnd ?? s;
    field.value = field.value.slice(0, s) + b.dataset.token + field.value.slice(t);
    field.focus(); field.selectionStart = field.selectionEnd = s + b.dataset.token.length;
  }));

  // drop zone: show the chosen filename, accept drag and drop
  document.querySelectorAll('.drop').forEach(zone => {
    const input = zone.querySelector('input[type=file]');
    const name = zone.querySelector('.file');
    const show = () => { name.textContent = input.files.length ? input.files[0].name : 'No file chosen yet'; };
    input.addEventListener('change', show);
    ['dragenter', 'dragover'].forEach(t => zone.addEventListener(t, e => { e.preventDefault(); zone.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => zone.addEventListener(t, () => zone.classList.remove('over')));
    zone.addEventListener('drop', e => { e.preventDefault(); if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); } });
  });
})();
