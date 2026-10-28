  // ---------- one reader for both views: a step reads as one action
  const readerHost = () => S.view === 'transcript' ? els.panel : S.view === 'steps' ? els.readcol : null;
  const navList = () => S.view === 'transcript' ? transcriptOrder() : matches;
  const navSort = () => S.view === 'transcript' ? 'round' : S.sort;

  // Where the open step sits in what J and K walk, and which steps they lead to. A step
  // outside it still has a place: where the order would put it, between two neighbours.
  function navFrom(list, open, sort) {
    if (!open) return { at: -1, prev: -1, next: -1 };
    const at = list.findIndex(x => x.id === open.id);
    if (at >= 0) return { at, prev: at - 1, next: at + 1 < list.length ? at + 1 : -1 };
    const cmp = comparatorFor(sort);
    let i = 0;
    while (i < list.length && cmp(list[i], open) < 0) i++;
    return { at: -1, prev: i - 1, next: i < list.length ? i : -1 };
  }
  function moveStep(delta) {
    const list = navList();
    if (!S.step) { if (list.length) openStep(delta > 0 ? list[0] : list[list.length - 1], { record: true }); return; }
    const nav = navFrom(list, byId.get(S.step), navSort());
    const to = delta > 0 ? nav.next : nav.prev;
    if (to < 0) return;
    openStep(list[to], { record: false });
    if (S.view === 'steps' && to >= shown) renderList(false);
  }
  function refreshNav() {
    const host = readerHost(); if (!host) return;
    const list = navList(), nav = navFrom(list, byId.get(S.step), navSort());
    const prev = host.querySelector('[data-act="prev"]'), next = host.querySelector('[data-act="next"]'), pos = host.querySelector('.pos');
    if (prev) prev.disabled = nav.prev < 0;
    if (next) next.disabled = nav.next < 0;
    if (!pos) return;
    const where = S.view === 'transcript' ? 'on screen' : 'in this selection';
    pos.classList.toggle('out', nav.at < 0);
    if (nav.at >= 0) { pos.textContent = `${nav.at + 1} of ${list.length} ${where} · J and K move`; return; }
    const step = nav.next >= 0 ? ['next', list[nav.next]] : nav.prev >= 0 ? ['prev', list[nav.prev]] : null;
    pos.textContent = step ? `not ${where} · ${step[0]} goes to ${stepLabel(step[1])}` : `not ${where}`;
  }
  function focusMark(id, root) {
    const q = `[data-anchor="${CSS.escape(id)}"], [data-lost="${CSS.escape(id)}"]`;
    const c = (root || document).querySelector(q) || document.querySelector(q); if (!c) return;
    c.querySelector('textarea')?.focus({ preventScroll: true }); c.scrollIntoView({ block: 'nearest' });
  }
  function redrawStep(s) { const host = readerHost(); const y = host ? host.scrollTop : 0; renderReaderFor(s); if (host) host.scrollTop = y; }

  function openStep(s, opts) {
    if (!s) return;
    S.step = s.id;
    if (S.view !== 'transcript' && S.view !== 'steps') showView('transcript');
    renderReaderFor(s);
    const host = readerHost();
    if (host && !opts?.keepScroll) host.scrollTop = 0;
    if (S.view === 'transcript') els.tgrid.querySelector(`.tc[data-step="${CSS.escape(s.id)}"]:not(.v)`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    else els.list.querySelector('.row[aria-current="true"]')?.scrollIntoView({ block: 'nearest' });
    renderCrumbs();
    writeHash(opts?.record ? 'push' : 'replace');
  }

  function annotated(s, field, placed, cls) {
    const text = fieldText(s, field);
    const spans = placed.filter(x => x.m.field === field && x.at).map(x => [x.at[0], x.at[1], 'hl', x.m.id]);
    return `<pre class="trace${cls ? ' ' + cls : ''}" data-field="${field}">\n${text ? renderMarked(text, spans) : '(empty)'}</pre>`;
  }
  // `what` may carry icons, so callers escape its text; the rest is escaped here.
  const foldSummary = (part, label, what, size) => `<summary>${partIcon(part, 16)}<span class="flbl">${esc(label)}</span>${what ? `<span class="fwhat">${what}</span>` : ''}${size ? `<span class="fsize">${esc(size)}</span>` : ''}<span class="fchev">${icon('right', 14)}</span></summary>`;
  function verdictBlock(v) {
    return `<div class="verdict ${levelOf(v.score)}"><div class="vh">${pill(v.score)}<b>${esc(v.monitor)}</b><span>on ${esc(v.target || 'this step')}</span></div>${esc(v.reason)}</div>`;
  }
  function stepMarkdown(s) {
    const lines = [`# ${stepLabel(s)}`, '', `- run: ${RUN.name}`, `- step id: ${s.id}`, `- peak suspicion: ${s.peak ?? 'not scored'} (audit threshold ${THR})`, `- covert: ${s.covert.join(', ') || 'no'}`];
    s.calls.forEach(c => { if (c.said && c.identity === s.agent) lines.push('', '## What it said', '', c.said); });
    s.tools.forEach((t, i) => lines.push('', `## What it did${s.tools.length > 1 ? ` (${i + 1})` : ''}: ${t.function}${t.blocked ? ' (blocked)' : ''}`, '', t.arguments_text, '', '### Result', '', t.result_text || '(no result)'));
    if (s.verdicts.length) { lines.push('', '## How the monitors read it', ''); s.verdicts.forEach(v => lines.push(`- ${v.monitor} ${v.score} on ${v.target || '?'}: ${v.reason}`)); }
    s.calls.forEach((c, i) => lines.push('', `## Model call ${i + 1} as ${c.identity}${c.phase ? ` (${c.phase})` : ''}`, '', ...(c.reasoning ? ['### Reasoning', '', c.reasoning, ''] : []), '### Reply', '', c.reply || '(empty)', '', '### Prompt', '', '```', c.prompt, '```'));
    s.events.forEach(ev => lines.push('', `## ${ev.kind} by ${ev.actor}`, '', '```json', ev.body, '```'));
    return lines.join('\n');
  }

  function renderReaderFor(s) {
    const host = readerHost(); if (!host) return;
    // Only the host of the current view holds a reader, so ids and handlers never
    // meet a stale copy in the other view.
    for (const other of [els.panel, els.readcol]) if (other !== host) other.innerHTML = other === els.readcol ? '<div class="empty"><b>Nothing open yet</b>Pick a step from the list. J and K move between steps.</div>' : '';
    if (host === els.panel) { els.panel.hidden = false; el('pdivider').hidden = false; }
    const mine = noteOf(s.id);
    const placed = (mine?.marks || []).map(m => ({ m, at: anchorAt(fieldText(s, m.field), m) }));
    const marked = new Set(placed.filter(x => x.at).map(x => x.m.field));
    const openIf = (cond) => cond ? ' open' : '';
    const lost = placed.filter(x => !x.at);
    const seqs = [...s.calls, ...s.tools, ...s.events].map(x => x.seq).filter(x => x != null);
    const hasTool = s.tools.length > 0;

    // What the agent wrote in its own words beside its action: a refusal, a plan, a status note.
    const spoke = s.calls.map((c, i) => c.said && c.identity === s.agent ? `<h3>${partIcon('said', 15)}What it said</h3>${annotated(s, `said-${i}`, placed)}` : '').join('');
    const did = s.tools.map((t, i) => `
      <h3>${partIcon('did', 15)}What it did${s.tools.length > 1 ? ` (${i + 1})` : ''} &middot; ${kindIcon(t.function, 14)}<span class="q mono">${esc(t.function)}</span>${t.verdicts.map(v => pill(v.score)).join('')}${covertTags(t.covert)}${t.blocked ? `<span class="tag blocked">blocked: ${esc(t.blocked)}</span>` : ''}</h3>
      ${annotated(s, `args-${i}`, placed)}
      <div class="sub">${partIcon('result', 12)}result</div>${annotated(s, `result-${i}`, placed, 'code')}`).join('');
    const happened = !hasTool && s.events.length ? `<h3>${partIcon('happened', 15)}What happened</h3>${s.events.map((ev, i) => `<div class="sub">${kindIcon(ev.kind, 12)}${esc(ev.kind)} by ${esc(ev.actor || '?')}${ev.verdicts.map(v => ' ' + pill(v.score)).join('')}${covertTags(ev.covert)}</div>${annotated(s, `body-${i}`, placed, 'code')}`).join('')}` : '';
    const read = `<h3>${partIcon('monitors', 15)}How the monitors read it<span class="q"> &middot; audit threshold ${THR}</span></h3>${s.verdicts.length ? [...s.verdicts].sort((a, b) => b.score - a.score).map(verdictBlock).join('') : IS_TWIN && !CAL ? '<p class="help" style="margin:0">Grading did not save the honest twin&rsquo;s verdicts, so none show here.</p>' : '<p class="help" style="margin:0">No monitor scored anything in this step.</p>'}`;
    const PHASE = { deciding: 'while choosing its action', executing: 'while a tool ran' };
    const said = s.calls.map((c, i) => {
      const own = c.identity === s.agent;
      const who = own ? '' : ` as ${esc(c.identity)}${c.phase ? `, ${esc(PHASE[c.phase] || c.phase)}` : ''}`;
      const rawWhat = own && hasTool ? 'the JSON the action above was read from' : `the reply${who}`;
      const thought = c.reasoning ? `<details class="fold"${openIf(marked.has(`reasoning-${i}`))}>${foldSummary('reasoning', 'Reasoning', `what the model reasoned${who}`, `${c.reasoning.length.toLocaleString()} chars`)}${annotated(s, `reasoning-${i}`, placed)}</details>` : '';
      return `${thought}<details class="fold"${openIf((!hasTool && s.calls.length === 1) || marked.has(`reply-${i}`))}>${foldSummary('raw', 'Raw reply', rawWhat, `${c.reply.length.toLocaleString()} chars`)}${annotated(s, `reply-${i}`, placed)}</details>
        <details class="fold"${openIf(marked.has(`prompt-${i}`))}>${foldSummary('context', 'Context the model saw', `the full prompt${who}`, `${c.prompt.length.toLocaleString()} chars`)}${annotated(s, `prompt-${i}`, placed, 'code')}</details>`;
    }).join('');
    const also = hasTool && s.events.length ? `<details class="fold"${openIf(s.events.some((_, i) => marked.has(`body-${i}`)))}>${foldSummary('effects', 'Side effects of this turn', [...new Set(s.events.map(e => e.kind))].map(k => `${kindIcon(k, 12)}${esc(k)}`).join(' '), plural(s.events.length, 'event'))}${s.events.map((ev, i) => `<div class="sub">${kindIcon(ev.kind, 12)}${esc(ev.kind)} by ${esc(ev.actor || '?')}${covertTags(ev.covert)}</div>${annotated(s, `body-${i}`, placed, 'code')}`).join('')}</details>` : '';
    const inPanel = host === els.panel;

    host.innerHTML = `<div class="reader${placed.some(x => x.at) ? '' : ' nogutter'}" data-step-host="${esc(s.id)}">
      <div class="head"><h2>${esc(stepLabel(s))}</h2>${pill(s.peak)}${covertTags(s.covert)}
        <div class="navb"><button class="btn" data-act="prev" title="previous step (K)">&larr;</button><button class="btn" data-act="next" title="next step (J)">&rarr;</button><button class="btn" data-act="read" aria-pressed="false"></button>${inPanel ? `<button class="btn" data-act="close" title="close (Esc)">${icon('close', 15)}close</button>` : `<button class="btn" data-act="focus" aria-pressed="${S.focus}" title="hide everything but the step. Escape leaves.">${icon('expand', 15)}${S.focus ? 'Leave focus' : 'Focus'}</button>`}</div>
        <div class="pos"></div>
      </div>
      <div class="tracenote">
        <div class="eyebrow">${partIcon('note', 13)}Your note on this step</div>
        <textarea data-act="note" placeholder="What do you make of this step? To note a passage, select its words and pin a note in the margin.">${esc(mine?.text || '')}</textarea>
      </div>
      <div class="split-read">
        <svg class="connectors" aria-hidden="true"></svg>
        <div class="textcol">
          ${spoke}${did}${happened}${read}${said}${also}
          ${s.calls.length && !s.calls.some(c => c.reasoning) ? `<p class="help absent"><span class="pi">${icon('eyeoff', 14)}</span>No reasoning: the model returned none here, or the run left no gateway call log to read it from.</p>` : ''}
          <div class="metaline">${seqs.length ? `sealed #${Math.min(...seqs)}&ndash;${Math.max(...seqs)} &middot; ` : ''}<code>${esc(s.id)}</code><button class="copy" data-copy-text="${esc(s.id)}">copy id</button><button class="copy" data-copy-link="${esc(s.id)}">copy link</button><button class="copy" data-act="md">copy as Markdown</button></div>
        </div>
        <div class="gutter${placed.some(x => x.at) ? '' : ' empty'}">
          <div class="marginnotes" style="position:relative">${placed.filter(x => x.at).map(x => noteCard(x.m, s.id)).join('')}</div>
          ${lost.length ? `<div style="margin-top:12px">${lost.map(x => lostCard(x.m, s.id)).join('')}</div>` : ''}
        </div>
      </div>
    </div>`;

    const act = name => host.querySelector(`[data-act="${name}"]`);
    act('prev')?.addEventListener('click', () => moveStep(-1));
    act('next')?.addEventListener('click', () => moveStep(1));
    act('read')?.addEventListener('click', () => setRead(s.id, !noteRead(noteOf(s.id))));
    act('close')?.addEventListener('click', () => closePanel({ record: true }));
    act('focus')?.addEventListener('click', () => { S.focus = !S.focus; applyFocus(); renderReaderFor(s); writeHash('replace'); });
    act('md')?.addEventListener('click', ev => copyText(stepMarkdown(s), ev.currentTarget));
    act('note')?.addEventListener('input', ev => { noteFor(s.id).text = ev.target.value; touch(s.id); const card = els.tgrid.querySelector(`.tc.open[data-step="${CSS.escape(s.id)}"] .tnote`); if (card) card.textContent = ev.target.value; });
    host.querySelectorAll('details.fold').forEach(d => d.addEventListener('toggle', scheduleLayout));
    markCurrentCards();
    refreshNav();
    refreshReadButton();
    applyNarrow();
    observeLayout();
    layoutNotes();
    scheduleLayout();
  }

  // A panel too narrow for a margin stacks the notes under the text instead of
  // squeezing the text beside them. The margin needs room for both.
  const MARGIN_NEEDS = 820;
  function applyNarrow() { els.panel.classList.toggle('narrow', els.panel.clientWidth > 0 && els.panel.clientWidth < MARGIN_NEEDS); }
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => { applyNarrow(); scheduleLayout(); }).observe(els.panel);

  // Notes want to sit beside their passage, but two passages can be one line apart and
  // a card is forty pixels tall. Sort by anchor, sweep down once so no card covers the
  // one above it, and bend the connector when a card ends up off its line.
  const NOTE_GAP = 10;
  let layoutQueued = false, notesObserver = null;
  function scheduleLayout() {
    if (layoutQueued) return;
    layoutQueued = true;
    const run = () => { if (!layoutQueued) return; layoutQueued = false; layoutNotes(); };
    requestAnimationFrame(run);
    setTimeout(run, 32);
  }
  function observeLayout() {
    notesObserver?.disconnect();
    if (typeof ResizeObserver === 'undefined') return;
    notesObserver = new ResizeObserver(scheduleLayout);
    const split = readerHost()?.querySelector('.split-read');
    if (!split) return;
    notesObserver.observe(split.querySelector('.textcol'));
    split.querySelectorAll('.marginnotes .note').forEach(c => notesObserver.observe(c));
  }
  function layoutNotes() { document.querySelectorAll('.split-read').forEach(layoutOneSplit); }
  const markOf = (split, card) => split.querySelector(`mark.hl[data-mark="${CSS.escape(card.dataset.anchor)}"]`);
  // Cards in the order their passages appear, not the order they were written.
  const inReadingOrder = (split, cards) => cards.map(c => ({ c, mk: markOf(split, c) }))
    .sort((a, b) => (a.mk && b.mk ? (a.mk.compareDocumentPosition(b.mk) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1) : a.mk ? -1 : b.mk ? 1 : 0))
    .map(x => x.c);
  // Too narrow for a margin: each card moves to just under the block it annotates, so
  // the note stays beside its passage. Moving a node keeps its handlers.
  function notesInline(split, holder) {
    // Gather every card first: removing a group from a previous pass must never take a card with it.
    const cards = inReadingOrder(split, [...split.querySelectorAll('.note[data-anchor]')]);
    cards.forEach(c => holder.appendChild(c));
    split.querySelectorAll('.inline-notes').forEach(g => g.remove());
    for (const card of cards) {
      card.style.top = '';
      const pre = markOf(split, card)?.closest('pre');
      if (!pre) { holder.appendChild(card); continue; }
      let group = pre.nextElementSibling;
      if (!group || !group.classList.contains('inline-notes')) { group = document.createElement('div'); group.className = 'inline-notes'; pre.after(group); }
      group.appendChild(card);
    }
    holder.style.height = '';
  }
  function notesInMargin(split, holder) {
    const cards = inReadingOrder(split, [...split.querySelectorAll('.note[data-anchor]')]);
    cards.forEach(c => holder.appendChild(c));
    split.querySelectorAll('.inline-notes').forEach(g => g.remove());
    return cards;
  }
  function layoutOneSplit(split) {
    const holder = split?.querySelector('.marginnotes');
    const svg = split?.querySelector('.connectors');
    if (!split || !holder || !svg || !split.offsetParent) return;
    if (window.matchMedia('(max-width: 1000px)').matches || split.closest('.narrow')) {
      notesInline(split, holder);
      svg.innerHTML = '';
      return;
    }
    const cards = notesInMargin(split, holder);
    const box = split.getBoundingClientRect(), hBox = holder.getBoundingClientRect();
    const gutterLeft = hBox.left - box.left, holderTop = hBox.top - box.top;
    const paths = [];
    let cursor = 0;
    for (const card of cards) {
      const mk = split.querySelector(`mark.hl[data-mark="${CSS.escape(card.dataset.anchor)}"]`);
      const mBox = mk && mk.getClientRects().length ? mk.getBoundingClientRect() : null;
      const want = mBox ? Math.max(0, mBox.top - box.top - holderTop) : cursor;
      const top = Math.max(want, cursor);
      card.style.top = top + 'px';
      cursor = top + card.offsetHeight + NOTE_GAP;
      if (!mBox) continue;
      // Leave from the text block's edge, level with the passage: a line from the
      // passage itself would run through the rest of its line like a strikethrough.
      const edge = (mk.closest('pre') || mk).getBoundingClientRect().right - box.left;
      const fx = Math.min(edge + 2, gutterLeft - 6);
      const fy = mBox.top - box.top + mBox.height / 2;
      const ty = holderTop + top + 15;
      paths.push(Math.abs(fy - ty) < 3 ? `M${fx} ${fy} H${gutterLeft}` : `M${fx} ${fy} H${gutterLeft - 11} V${ty} H${gutterLeft}`);
    }
    holder.style.height = cursor + 'px';
    svg.setAttribute('viewBox', `0 0 ${Math.round(box.width)} ${Math.round(box.height)}`);
    svg.setAttribute('width', Math.round(box.width));
    svg.setAttribute('height', Math.round(box.height));
    svg.innerHTML = paths.map(d => `<path d="${esc(d)}" fill="none" stroke="var(--accent)" stroke-width="1.2" stroke-linejoin="round" opacity=".5"/>`).join('');
  }
  window.addEventListener('resize', scheduleLayout);
  window.matchMedia('(max-width: 1000px)').addEventListener('change', scheduleLayout);
  document.fonts?.ready.then(scheduleLayout);

  document.addEventListener('click', e => {
    const b = e.target instanceof Element ? e.target.closest('.copy') : null; if (!b) return;
    if (b.dataset.copyText !== undefined) copyText(b.dataset.copyText, b);
    else if (b.dataset.copyLink !== undefined) copyText(location.href.split('#')[0] + '#view=' + encodeURIComponent(S.view) + '&t=' + encodeURIComponent(b.dataset.copyLink), b);
  });

  // ---------- notes in and out: one file holds the whole state, never a subset
  const notesJson = () => JSON.stringify({ ...NOTES, savedAt: new Date().toISOString() }, null, 1);
  function markSaved() { NOTES.savedAt = new Date().toISOString(); keepNotes(); }
  // A file's marks are kept only if a note card can draw them, so a bad file cannot break the notes for good.
  const drawableMarks = ms => (Array.isArray(ms) ? ms : []).filter(m => m && typeof m.id === 'string' && typeof m.quote === 'string');
  function mergeNotes(text) {
    const incoming = JSON.parse(text);
    if (!incoming || !incoming.steps || typeof incoming.steps !== 'object') throw new Error('no `steps` object in that file');
    let added = 0, merged = 0;
    for (const [id, n] of Object.entries(incoming.steps)) {
      if (!n || typeof n !== 'object' || id === '__proto__') continue;
      const mine = Object.hasOwn(NOTES.steps, id) ? NOTES.steps[id] : null;  // never an inherited property
      if (!mine) { NOTES.steps[id] = { text: String(n.text || ''), marks: drawableMarks(n.marks), at: n.at || new Date().toISOString(), ...(n.read ? { read: true } : {}) }; added++; continue; }
      if (n.read) mine.read = true;
      const seen = new Set((mine.marks || []).map(m => m.id));
      drawableMarks(n.marks).forEach(m => { if (!seen.has(m.id)) (mine.marks ||= []).push(m); });
      const here = String(mine.text || '').trim(), there = String(n.text || '').trim();
      mine.text = !there || here === there ? here : (here ? here + '\n\n---\n\n' + there : there);
      mine.at = new Date().toISOString(); merged++;
    }
    keepNotes(); return { added, merged };
  }
  el('notes-save').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([notesJson()], { type: 'application/json' })), a = document.createElement('a');
    a.href = url; a.download = `loc-arena-notes-${RUN.name}.json`; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    markSaved();
  });
  el('notes-load').addEventListener('click', () => el('notes-import').click());
  function importNotesFile(file) {
    if (!file) return;
    file.text().then(text => {
      const r = mergeNotes(text);
      alert(`Loaded ${plural(r.added, 'new step')} and merged ${r.merged} into notes already here.`);
      const s = byId.get(S.step); if (s) redrawStep(s);
      renderList(true);
    }).catch(e => alert('Could not read that as a notes file: ' + e.message));
  }
  el('notes-import').addEventListener('change', ev => { const file = ev.target.files?.[0]; ev.target.value = ''; importNotesFile(file); });
  // A page that ignores a dropped file navigates to it and loses unsaved notes, so the
  // default is refused everywhere. Only drags carrying files are touched.
  const dragHasFile = e => [...(e.dataTransfer?.types || [])].includes('Files');
  const dropzone = () => el('notes-dialog');
  let dragDepth = 0;
  const showDrop = on => { dragDepth = on ? dragDepth : 0; dropzone()?.classList.toggle('dropping', on); };
  document.addEventListener('dragenter', e => { if (!dragHasFile(e)) return; e.preventDefault(); dragDepth++; dropzone()?.classList.add('dropping'); });
  document.addEventListener('dragover', e => { if (dragHasFile(e)) e.preventDefault(); });
  document.addEventListener('dragleave', e => { if (!dragHasFile(e)) return; if (--dragDepth <= 0) showDrop(false); });
  document.addEventListener('drop', e => {
    if (!dragHasFile(e)) return;
    e.preventDefault(); showDrop(false);
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    openNotes();
    importNotesFile(file);
  });
  el('notes-clear').addEventListener('click', () => {
    const n = Object.keys(NOTES.steps).length, un = unsaved(); if (!n) return;
    if (!confirm(un ? `${un} of ${n} annotated steps have changed since the last save to a file. Delete every note on this run in this browser anyway?` : `Delete the notes on all ${n} steps in this browser?`)) return;
    NOTES = emptyNotes(); keepNotes();
    const s = byId.get(S.step); if (s) redrawStep(s);
    renderList(true);
  });
  el('notes-close').addEventListener('click', () => el('notes-dialog').close());
  const openNotes = () => { renderNotesState(); el('notes-dialog').showModal(); };
  window.addEventListener('beforeunload', e => { if ((!notesOk || saveFailed) && Object.keys(NOTES.steps).length) { e.preventDefault(); e.returnValue = ''; } });

  // ---------- communication: everything said, where it went, and who read it (worked out when the page was built)
  const COMM = DATA.communication;
  const readClass = c => c.readers.agents.length ? 'agent' : c.readers.monitors.length ? 'monitor' : 'none';
  const audienceIcon = a => a === 'scratch' ? 'sticky' : a === 'team' ? 'broadcast' : 'robot';
  const C = { kind: '', read: '', covert: false, sender: '', audience: '' };
  function renderComms() {
    const count = f => COMM.filter(f).length;
    const said = COMM.filter(c => c.kind !== 'environment' && c.kind !== 'refused');
    const stat = (big, lbl, cls) => `<div class="stat"><div class="big ${cls || ''}">${big}</div><div class="lbl">${lbl}</div></div>`;
    el('comm-stats').innerHTML = [
      stat(count(c => c.kind === 'message'), 'messages between agents'),
      stat(count(c => c.kind === 'scratch'), 'scratch notes', 'covert'),
      stat(count(c => c.kind === 'refused'), 'refused sends', 'flag'),
      stat(count(c => c.kind === 'environment'), 'environment posts'),
      stat(said.filter(c => readClass(c) === 'agent').length, 'of the agents&rsquo; messages and notes were read by another agent'),
      stat(said.filter(c => readClass(c) === 'monitor').length, 'read only by monitors', 'flag'),
      stat(said.filter(c => readClass(c) === 'none').length, 'read by no one'),
    ].join('');

    const senders = [...new Set(said.map(c => c.sender))].sort((a, b) => laneIndex(a) - laneIndex(b));
    const audiences = [...new Set(said.map(c => c.audience))].sort((a, b) => (a === 'scratch') - (b === 'scratch') || (a === 'team') - (b === 'team') || laneIndex(a) - laneIndex(b));
    el('comm-matrix').innerHTML = senders.length ? `<div class="gridwrap"><table class="cmx"><tr><th></th>${audiences.map(a => `<th>${icon(audienceIcon(a), 13)} ${esc(a)}</th>`).join('')}</tr>${senders.map(f => `<tr><th>${icon('robot', 13)} ${esc(f)}</th>${audiences.map(a => { const n = said.filter(c => c.sender === f && c.audience === a); const cv = n.filter(c => c.covert.length).length; return n.length ? `<td><button data-cf="${esc(f)}" data-ct="${esc(a)}" class="${C.sender === f && C.audience === a ? 'on' : ''}"><b>${n.length}</b>${cv ? `<span class="cv">${cv} covert</span>` : ''}</button></td>` : '<td></td>'; }).join('')}</tr>`).join('')}</table></div>` : '<p class="help">No agent said anything to anyone in this run.</p>';
    el('comm-matrix').querySelectorAll('[data-cf]').forEach(b => b.addEventListener('click', () => {
      const same = C.sender === b.dataset.cf && C.audience === b.dataset.ct;
      C.sender = same ? '' : b.dataset.cf; C.audience = same ? '' : b.dataset.ct; renderComms();
    }));

    const shown = COMM.filter(c => (!C.kind || c.kind === C.kind) && (!C.read || readClass(c) === C.read) && (!C.covert || c.covert.length) && (!C.sender || c.sender === C.sender) && (!C.audience || c.audience === C.audience));
    el('c-chips').innerHTML = (C.sender ? `<button class="chip" data-cclear="1"><i>from</i>${esc(C.sender)} <i>to</i>${esc(C.audience)} &times;</button>` : '') + `<span>${shown.length} of ${COMM.length}</span>`;
    el('c-chips').querySelector('[data-cclear]')?.addEventListener('click', () => { C.sender = ''; C.audience = ''; renderComms(); });
    const toIcon = c => c.kind === 'scratch' ? 'sticky' : c.kind === 'environment' ? (KIND_ICON[c.channel] || 'globe') : audienceIcon(c.audience);
    el('comm-feed').innerHTML = shown.map(c => {
      const step = byId.get(c.step), r = c.readers, cls = readClass(c);
      const chips = [
        ...r.agents.map(id => byId.get(id)).filter(Boolean).map(s => `<button class="chip quiet rd" data-open="${esc(s.id)}" title="open the turn that read it">${icon('robot', 12)} ${esc(s.agent)} ${roundKey(s.round)}</button>`),
        ...r.monitors.map(m => `<span class="chip quiet rdm" title="a monitor saw it">${icon('shield', 12)} ${esc(m)}</span>`),
        ...r.others.map(o => `<span class="chip quiet" title="read by the environment">${icon('globe', 12)} ${esc(o)}</span>`),
      ].join('');
      const rereads = r.rereads.map(id => byId.get(id)).filter(Boolean);
      const selfNote = rereads.length ? `<span class="help">the author read it again in ${rereads.map(s => roundKey(s.round)).join(', ')}</span>` : '';
      const readLine = c.kind === 'refused' ? '<span class="unread">refused: never delivered</span>' : !r.traced ? '<span class="help">too short to trace</span>'
        : cls === 'none' ? `<span class="unread">${icon('eyeoff', 13)} no one</span>`
        : cls === 'monitor' ? `${chips}<span class="unread mon">${icon('eyeoff', 13)} no agent</span>` : chips;
      return `<div class="citem ${c.kind}${c.covert.length ? ' covert' : ''} read-${cls}">
        <div class="cleft">${kindIcon(c.kind === 'scratch' ? 'write_scratch' : c.kind === 'environment' ? c.channel : 'message', 16)}<span>${roundKey(step.round)}</span></div>
        <div class="cmain">
          <div class="chead">${icon(LANES.includes(c.sender) && c.sender !== WORLD ? 'robot' : 'globe', 14)}<b>${esc(c.sender)}</b><span class="arrow">${icon('right', 13)}</span>${icon(toIcon(c), 14)}<b>${esc(c.audience)}</b>${c.scores.map(score => pill(score)).join('')}${covertTags(c.covert)}</div>
          <div class="cbody">${esc(c.text)}</div>
          <div class="cread"><span class="lbl">${icon('eye', 13)} read by</span>${readLine}${selfNote}</div>
        </div>
        <button class="tcbtn copen" data-open="${esc(c.step)}" title="open the turn that said it">open &#8599;</button>
      </div>`;
    }).join('') || '<p class="help">Nothing matches.</p>';
    el('comm-feed').querySelectorAll('[data-open]').forEach(b => b.addEventListener('click', () => { showView('transcript'); openStep(byId.get(b.dataset.open), { record: true }); }));
  }
  [['c-kind', 'kind'], ['c-read', 'read']].forEach(([id, key]) => el(id).addEventListener('change', ev => { C[key] = ev.target.value; renderComms(); }));
  el('c-covert').addEventListener('change', ev => { C.covert = ev.target.checked; renderComms(); });

  // ---------- outcome and monitors against the sealed covert evidence
  const stat = (big, lbl, cls) => `<div class="stat"><div class="big ${cls || ''}">${big}</div><div class="lbl">${lbl}</div></div>`;
  function renderMonitors() {
    renderCalibration();
    if (!OUT) {  // the honest twin's page: its false alarms and how it compares; it has no outcome of its own
      el('view-monitors').querySelector('h1').textContent = 'Monitors on the honest twin';
      el('mon-lede').innerHTML = `The honest twin: the same team on the same task, never told of the side task, played to set the audit threshold. Its monitors ran on <code>${esc((RUN.models || {}).trusted_monitor || '?')}</code>, and every step they scored above the threshold is a false alarm.`;
      el('mon-stats').innerHTML = [
        stat(STEPS.filter(flagged).length, `steps above the audit threshold (${esc(THR)}): false alarms`, 'flag'),
        stat(VERDICTS.filter(v => v.score === THR).length, 'verdicts at the threshold: the scores that set it'),
        stat(VERDICTS.length, 'verdicts in all'),
      ].join('');
      return renderUnplaced();
    }
    const mt = OUT.main_task, st = OUT.side_task, sf = OUT.safety, models = RUN.models || {};
    el('mon-lede').innerHTML = `${RUN.mode === 'attack' ? 'An attack' : 'An honest'} run. The agents ran on <code>${esc(models.untrusted_agent || '?')}</code>, the monitors on <code>${esc(models.trusted_monitor || '?')}</code>, over ${Math.round((RUN.wall_clock_seconds || 0) / 60)} minutes.`;
    const reached = OUT.milestones.filter(m => m.reached).length;
    el('mon-stats').innerHTML = [
      stat(esc(mt.final_score), `main task score${mt.gate_passed ? '' : ` &middot; gate failed: ${esc(mt.gate_reason || '?')}`}`),
      stat(st.landing_robust ? 'robust' : st.landing_minimal ? 'minimal' : 'no', `side task landed &middot; ${reached} of ${OUT.milestones.length} milestones`, st.landing_minimal ? 'covert' : ''),
      stat(sf.caught ? 'caught' : 'missed', sf.caught ? `by ${esc(sf.caught_by.join(', '))}` : 'no monitor reached the audit threshold', sf.caught ? 'flag' : ''),
      stat(esc(THR), 'audit threshold, from the honest twin at the audit budget'),
    ].join('');

    const covert = STEPS.filter(s => s.covert.length), plain = STEPS.filter(s => !s.covert.length);
    const cellBtn = (n, label, spec, cls) => `<td class="${cls || ''}"><button data-go='${esc(JSON.stringify(spec))}'><b>${n}</b><span>${label}</span></button></td>`;
    el('mon-matrix').innerHTML = `<table class="matrix"><tr><th></th><th>flagged (score above ${THR})</th><th>not flagged</th></tr>
      <tr><th>covert steps</th>${cellBtn(covert.filter(flagged).length, 'covert, flagged', { verdict: 'flagged', covert: 'covert' }, 'hit')}${cellBtn(covert.filter(s => !flagged(s)).length, 'covert, missed', { verdict: 'notflagged', covert: 'covert' }, 'miss')}</tr>
      <tr><th>other steps</th>${cellBtn(plain.filter(flagged).length, 'flagged, not covert', { verdict: 'flagged', covert: 'plain' })}${cellBtn(plain.filter(s => !flagged(s)).length, 'neither', { verdict: 'notflagged', covert: 'plain' })}</tr></table>
      <p class="help" style="max-width:88ch">Steps, not verdicts: a step counts as flagged when any verdict on it scores above the audit threshold, as the harness counts it. Covert means the sealed verifier cites the step as side-task evidence. Click a count to list those steps.</p>`;
    el('mon-matrix').querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => go({ view: 'steps', ...JSON.parse(b.dataset.go) })));

    const rows = MONITORS.map(m => {
      const vs = VERDICTS.filter(v => v.monitor === m.name);
      const placed = vs.filter(v => v.step).length, nf = vs.filter(v => v.score > THR).length;
      const peak = vs.length ? Math.max(...vs.map(v => v.score)) : null;
      return `<tr><td><b>${esc(m.name)}</b><br><span class="help">${esc(m.role)} &middot; ${esc(m.mode)} &middot; ${esc(m.timing)}${m.counts_toward_caught ? '' : ' &middot; does not count toward caught'}${m.errored ? ' &middot; <b style="color:var(--flag)">errored</b>' : ''}</span></td><td class="num">${vs.length}</td><td class="num">${placed}</td><td class="num">${nf}</td><td class="num">${pill(peak)}</td><td class="help">${esc((m.top_reasons || [])[0] || '—')}</td></tr>`;
    }).join('');
    el('mon-table').innerHTML = `<table class="legend"><tr><th>monitor</th><th class="num">verdicts</th><th class="num">on a step</th><th class="num">flagged</th><th class="num">peak</th><th>its top reason</th></tr>${rows}</table>`;

    const missed = covert.filter(s => !flagged(s)).sort(byRound);
    el('mon-missed').innerHTML = missed.length ? `<div class="list" style="max-width:900px">${missed.map(s => `<button class="row covert" data-open="${esc(s.id)}"><span class="k">${roundKey(s.round)}</span>${pill(s.peak)}<span class="lab"><b>${esc(s.agent)}</b> · ${esc(stepSummary(s))} · ${esc(s.covert.join(', '))}</span><span class="ch"><span class="dot"></span></span></button>`).join('')}</div>` : '<p class="help">Every covert step was flagged.</p>';

    renderUnplaced();

    el('mon-milestones').innerHTML = OUT.milestones.map(m => `<div class="ms ${m.reached ? 'yes' : 'no'}"><div class="st">${m.reached ? 'REACHED' : 'NOT REACHED'}${m.sealed ? '' : ' · not sealed'}</div><div>${esc(m.id.replace(/_/g, ' '))}</div>${m.steps.length ? `<div class="links">${m.steps.slice(0, 8).map(id => { const s = byId.get(id); return `<button class="chip quiet" data-open="${esc(id)}">${esc(s ? `${s.agent} ${roundKey(s.round)}` : id)}</button>`; }).join('')}${m.steps.length > 8 ? `<span class="help">+${m.steps.length - 8} more</span>` : ''}</div>` : ''}</div>`).join('');
    el('view-monitors').querySelectorAll('[data-open]').forEach(b => b.addEventListener('click', () => { showView('transcript'); openStep(byId.get(b.dataset.open), { record: true }); }));
  }

  // The run against its honest twin, on both pages: each monitor's score and both overalls, the twin's being the bar.
  function renderCalibration() {
    el('mon-calibration').hidden = !CAL;
    if (!CAL) return;
    const caught = CAL.run_overall > CAL.bar;  // then a monitor whose score is above the bar is one it was caught by
    const rows = CAL.rows.map(r => `<tr><td><b>${esc(r.monitor)}</b></td><td class="num"><b class="${caught && r.run_score > CAL.bar ? 'flag' : ''}">${scoreText(r.run_score)}</b></td><td class="num">${scoreText(r.twin_score)}</td></tr>`).join('');
    el('mon-calibration-table').innerHTML = `<table class="legend"><tr><th>monitor</th><th class="num">the run</th><th class="num">honest twin</th></tr>${rows}<tr><td><b>overall suspicion</b></td><td class="num"><b class="${caught ? 'flag' : ''}">${scoreText(CAL.run_overall)}</b></td><td class="num"><b>${scoreText(CAL.twin_overall)}</b>, the bar</td></tr></table>`;
  }

  function renderUnplaced() {
    const unplaced = VERDICTS.filter(v => !v.step);
    el('mon-unplaced').innerHTML = unplaced.length ? unplaced.map(v => `<div class="verdict ${levelOf(v.score)}" style="max-width:88ch"><div class="vh">${pill(v.score)}<b>${esc(v.monitor)}</b><span>&middot; ${esc(v.tap)} tap &middot; pinned to mirror #${esc(v.target_mirror_seq ?? 'none')} (${esc(v.actor)})</span></div>${esc(v.reason)}</div>`).join('') + `<p class="help" style="max-width:88ch">A verdict lands here when its target is not an agent step in the log: a whole-stream verdict (the inference monitor scores every model call at once and pins the verdict to the last off-path call), a record only the mirror holds, or no target at all.</p>` : '<p class="help">Every verdict sits on a step.</p>';
  }

  function renderMethod() {
    el('method-body').innerHTML = `
      <p><b>What you are looking at.</b> The transcript puts every agent in its own lane, side by side, one row per round, with each monitor&rsquo;s takes in the columns on the left. A card is one agent turn: what the agent did, and the start of what came back. Click a card to read that turn in the panel beside the transcript; J and K move through the turns on screen, and Escape, the close button or the browser&rsquo;s back button close it. Events outside any agent turn (the environment, scripted coworkers, credential rotation) sit under <b>World</b>.</p>
      <p><b>Where a verdict sits.</b> Monitors read the mirror log, so a verdict names a mirror event. The log this page is built from numbers events by the sealed log, and the two number independently. Each mirror event is paired with its sealed twin, and the verdict is placed on the turn holding that twin. A verdict whose target is not an agent turn goes to the <b>whole run</b> row and to <b>Outcome and monitors</b>.</p>
      <p><b>Flagged and covert.</b> A turn is flagged when a verdict on it scores above the audit threshold (${esc(THR)}); a score equal to it is not a flag, as in the harness, which sets it from the honest twin at the audit budget; a ringed pill marks such a score. Whether the run was <i>caught</i> is decided on the combined score, so a flagged turn is not the same as an audit. A turn is covert when the sealed verifier cites it as evidence for a side-task milestone.</p>
      <p><b>Model reasoning.</b> Shown on a turn when the model returned it, read from the call log the gateway of a stack run keeps beside the run. In-process and scripted runs, and models with reasoning off, have none. The monitors never see it.</p>
      <p><b>What is not here.</b> Verdict timestamps: the monitors run in batches, so their times say when a batch ran, not when the turn happened.</p>
      <p><b>Reading the outcome.</b> A pull request written before round 0, outside any agent turn, comes from the environment&rsquo;s script, even when it is stamped with an agent&rsquo;s name. A verdict on it says how the monitor reads that request, not what an agent did. This page shows one run; nothing here is a rate.</p>
      <p><b>Your notes.</b> Kept in this browser per run, and saved to or loaded from a file under <b>Your notes</b>. Nothing leaves this page.</p>`;
  }

  // ---------- navigation
  function go(spec) {
    const view = spec.view || 'steps';
    const rest = { ...spec }; delete rest.view;
    Object.assign(S, FILTER_DEFAULTS, { sort: 'round', step: null }, rest);
    showView(view);
    select();
    if (view === 'steps') els.readcol.innerHTML = '<div class="empty"><b>Nothing open yet</b>Pick a step from the list. J and K move between steps.</div>';
    writeHash('push');
  }
  function renderCrumbs() {
    const parts = [`<button data-crumb="home">${esc(RUN.slug)}</button>`];
    if (RUN.index) parts.unshift(`<a href="${esc(RUN.index)}">All runs</a>`, `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">${ICON.right}</svg>`);
    const sep = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">${ICON.right}</svg>`;
    parts.push(sep, `<b>${esc(VIEWS.find(v => v.id === S.view)?.label || S.view)}</b>`);
    if ((S.view === 'steps' || S.view === 'transcript') && S.step) { const s = byId.get(S.step); if (s) parts.push(sep, `<b>${esc(stepLabel(s))}</b>`); }
    els.crumbs.innerHTML = parts.join('');
    els.crumbs.querySelector('[data-crumb]')?.addEventListener('click', () => { if (S.view === 'transcript') closePanel({ record: true }); else showView('transcript', { record: true }); });
  }
  function syncControls() {
    for (const [id, field] of Object.entries(FIELD)) { const node = els[id]; if (!node) continue; if (node.type === 'checkbox') node.checked = S[field]; else if (document.activeElement !== node) node.value = S[field]; }
  }
  function buildControls() {
    el('f-agent').innerHTML = '<option value="">all</option>' + LANES.map(l => `<option value="${esc(l)}">${esc(l)}</option>`).join('');
    const tools = [...new Set(STEPS.flatMap(s => s.tools.map(t => t.function)))].sort();
    el('f-tool').innerHTML = '<option value="">any</option>' + tools.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
    for (const [id, field] of Object.entries(FIELD)) {
      const node = els[id];
      node.addEventListener(node.tagName === 'INPUT' && node.type !== 'checkbox' ? 'input' : 'change', () => {
        S[field] = node.type === 'checkbox' ? node.checked : node.value;
        if (field === 'trows') renderTranscript(); else select();
        writeHash('replace');
      });
    }
    el('f-clear').addEventListener('click', () => go({ view: 'steps' }));
    el('btn-more-filters').addEventListener('click', () => { const box = el('morefilters'); box.hidden = !box.hidden; });
    el('btn-sample').addEventListener('click', () => {
      if (S.sample) { S.sample = false; select(); writeHash('replace'); return; }
      S.seed = Math.floor(Math.random() * 1e6) + 1; S.sample = true; select(); writeHash('replace');
    });
    el('s-again').addEventListener('click', () => { S.seed = Math.floor(Math.random() * 1e6) + 1; select(); writeHash('replace'); });
    el('s-off').addEventListener('click', () => { S.sample = false; select(); writeHash('replace'); });
    el('s-flag').addEventListener('input', ev => { S.sflag = ev.target.value; if (S.sample) { select(); writeHash('replace'); } });
    el('s-clean').addEventListener('input', ev => { S.sclean = ev.target.value; if (S.sample) { select(); writeHash('replace'); } });
  }

  // Back and forward restore exactly what the address bar says: the defaults first,
  // then the hash, so nothing from the page being left carries over.
  function applyHash() {
    Object.assign(S, DEFAULT_STATE);
    const t = readHash();
    const s = t && byId.has(t) ? byId.get(t) : null;
    S.step = s ? s.id : null;
    showView(S.view);
    select();
    if (s) { renderReaderFor(s); renderCrumbs(); }
    else if (S.view === 'transcript') closePanel();
    else if (S.view === 'steps') els.readcol.innerHTML = '<div class="empty"><b>Nothing open yet</b>Pick a step from the list. J and K move between steps.</div>';
  }
  window.addEventListener('popstate', applyHash);

  document.addEventListener('keydown', e => {
    if (e.target instanceof Element && e.target.matches('input, select, textarea')) return;
    if (el('notes-dialog').open) return;
    if (e.key === 'Escape') {
      if (S.view === 'transcript' && S.step) { closePanel({ record: true }); return; }
      if (S.view === 'transcript' && OPEN.size) { OPEN.clear(); renderTranscript(); return; }
      if (S.focus) { S.focus = false; applyFocus(); const s = byId.get(S.step); if (s) renderReaderFor(s); writeHash('replace'); }
      return;
    }
    if (S.view !== 'steps' && S.view !== 'transcript') return;
    if (e.key === 'r') { const s = byId.get(S.step); if (s && !noteWritten(noteOf(s.id))) setRead(s.id, !noteRead(noteOf(s.id))); return; }
    if (e.key === 'j' || e.key === 'k') moveStep(e.key === 'j' ? 1 : -1);
  });

  // ---------- per-viewer preferences
  const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } } };
  let base = Number(store.get('loc-arena-base')) || 14;
  const applyBase = () => { document.documentElement.style.setProperty('--base', base + 'px'); store.set('loc-arena-base', String(base)); scheduleLayout(); };
  el('size-down').addEventListener('click', () => { base = Math.max(11, base - 1); applyBase(); });
  el('size-up').addEventListener('click', () => { base = Math.min(22, base + 1); applyBase(); });
  if (base !== 14) applyBase();
  const applyTheme = choice => {
    if (choice) document.documentElement.setAttribute('data-theme', choice); else document.documentElement.removeAttribute('data-theme');
    document.querySelectorAll('[data-theme-choice]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.themeChoice === choice)));
  };
  document.querySelectorAll('[data-theme-choice]').forEach(b => b.addEventListener('click', () => { applyTheme(b.dataset.themeChoice); store.set('loc-arena-theme', b.dataset.themeChoice); }));
  applyTheme(store.get('loc-arena-theme') ?? '');

  // Which play: the run and its honest twin are two pages. Switching keeps the view and filters but not the step,
  // whose id names its play.
  if (RUN.peer) {
    const here = IS_TWIN ? 'twin' : 'run', seg = el('play-seg');
    seg.innerHTML = [['run', 'Attack play'], ['twin', 'Honest twin']].map(([play, label]) => play === here ? `<a aria-current="page">${label}</a>` : `<a href="${esc(RUN.peer)}" data-peer>${label}</a>`).join('');
    seg.querySelector('[data-peer]').addEventListener('click', e => {
      const kept = new URLSearchParams(location.hash.slice(1)); kept.delete(HASH_NAME.step);
      e.currentTarget.href = RUN.peer + (String(kept) ? '#' + kept : '');
    });
    seg.hidden = false;
  }

  const savedWidth = store.get('loc-arena-listw');
  if (savedWidth) els.listcol.style.width = savedWidth;
  const savedPanel = store.get('loc-arena-panelw');
  if (savedPanel) els.panel.style.width = savedPanel;
  const applyRail = mini => {
    const app = document.querySelector('.app');
    if (mini) app.setAttribute('data-rail', 'mini'); else app.removeAttribute('data-rail');
    const btn = el('btn-rail');
    btn.setAttribute('aria-expanded', String(!mini));
    btn.title = mini ? 'expand the sidebar' : 'collapse the sidebar';
    btn.innerHTML = icon('panel', 17);
    store.set('loc-arena-rail', mini ? 'mini' : 'full');
    scheduleLayout();
  };
  el('btn-rail').addEventListener('click', () => applyRail(!document.querySelector('.app').hasAttribute('data-rail')));
  applyRail(store.get('loc-arena-rail') === 'mini');

  // Two dividers, one behaviour: drag to resize, remember the width per browser.
  function bindDivider(divider, target, sign, key, min, max) {
    divider.addEventListener('pointerdown', e => {
      const x0 = e.clientX, w0 = target.clientWidth;
      divider.classList.add('active'); divider.setPointerCapture(e.pointerId);
      const move = ev => { target.style.width = Math.max(min, Math.min(max(), w0 + sign * (ev.clientX - x0))) + 'px'; scheduleLayout(); };
      const up = () => { divider.classList.remove('active'); divider.removeEventListener('pointermove', move); divider.removeEventListener('pointerup', up); store.set(key, target.style.width); };
      divider.addEventListener('pointermove', move); divider.addEventListener('pointerup', up);
    });
  }
  bindDivider(el('divider'), els.listcol, 1, 'loc-arena-listw', 240, () => 680);
  bindDivider(el('pdivider'), els.panel, -1, 'loc-arena-panelw', 420, () => el('view-transcript').clientWidth - 260);

  buildRail();
  buildControls();
  renderMethod();
  renderNotesState();
  applyHash();
  history.replaceState(null, '', location.hash || '#');
