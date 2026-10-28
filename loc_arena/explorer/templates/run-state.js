  const DATA = JSON.parse(document.getElementById('data').textContent);
  const RUN = DATA.run, OUT = DATA.outcome, STEPS = DATA.steps, VERDICTS = DATA.verdicts, MONITORS = DATA.monitors, LANES = DATA.lanes;
  const IS_TWIN = RUN.play === 'honest_cal';  // the honest twin's page: it is not scored on its own
  const CAL = DATA.calibration;  // the run against its honest twin, when grading kept the twin's scores
  document.body.classList.toggle('twin', IS_TWIN);
  const THR = RUN.audit_threshold;  // the honest twin's page has no outcome of its own, but the same threshold
  const WORLD = 'World';
  const byId = new Map(STEPS.map(s => [s.id, s]));
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = id => document.getElementById(id);

  const ICON = {
    grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.8"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.8"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.8"/>',
    list: '<line x1="9" y1="6.5" x2="20" y2="6.5"/><line x1="9" y1="12" x2="20" y2="12"/><line x1="9" y1="17.5" x2="20" y2="17.5"/><circle cx="4.6" cy="6.5" r="1.1" fill="currentColor" stroke="none"/><circle cx="4.6" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="4.6" cy="17.5" r="1.1" fill="currentColor" stroke="none"/>',
    shield: '<path d="M12 3.5 5 6.2v5.3c0 4.3 2.9 7.6 7 9 4.1-1.4 7-4.7 7-9V6.2Z"/><path d="M9 12.2l2.2 2.2 3.9-4"/>',
    pencil: '<path d="M11.5 19.5H20"/><path d="M15.8 4.4a2 2 0 0 1 2.9 2.8L7.6 18.3l-3.7.9.9-3.7Z"/>',
    help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.4a2.5 2.5 0 0 1 4.9.8c0 1.7-2.5 2.5-2.5 2.5"/><circle cx="12" cy="16.6" r=".9" fill="currentColor" stroke="none"/>',
    right: '<polyline points="9.5 5 16.5 12 9.5 19"/>',
    panel: '<rect x="3.5" y="4.2" width="17" height="15.6" rx="3"/><line x1="9.6" y1="4.2" x2="9.6" y2="19.8"/>',
    expand: '<polyline points="9.5 4.5 4.5 4.5 4.5 9.5"/><polyline points="14.5 4.5 19.5 4.5 19.5 9.5"/><polyline points="14.5 19.5 19.5 19.5 19.5 14.5"/><polyline points="9.5 19.5 4.5 19.5 4.5 14.5"/>',
    check: '<circle cx="12" cy="12" r="8.5"/><path d="M8 12.5l2.7 2.7L16 9.8"/>',
    close: '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>',
    warn: '<path d="M12 8.5v4.5"/><circle cx="12" cy="16.4" r=".9" fill="currentColor" stroke="none"/><path d="M10.3 4.4 3.1 17.2A2 2 0 0 0 4.8 20.2h14.4a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0Z"/>',
    trash: '<path d="M4.5 6.5h15"/><path d="M9.5 6.5V4.8a1.3 1.3 0 0 1 1.3-1.3h2.4a1.3 1.3 0 0 1 1.3 1.3v1.7"/><path d="M6.6 6.5 7.5 19a1.5 1.5 0 0 0 1.5 1.4h6a1.5 1.5 0 0 0 1.5-1.4l.9-12.5"/>',
  };
  // One icon per kind of thing, drawn in the same grammar as the rail, so a shape comes
  // to mean one thing wherever it appears: a tool, an event, a part of a step.
  Object.assign(ICON, {
    file: '<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8Z"/><polyline points="14 3.5 14 8 18.5 8"/>',
    folder: '<path d="M3.5 7.2a2 2 0 0 1 2-2h3.8l2 2.3h7.2a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z"/>',
    filepen: '<path d="M12.5 20.5h-5a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2H14l4.5 4.5v3"/><polyline points="14 3.5 14 8 18.5 8"/><path d="M19.2 13.8a1.4 1.4 0 0 1 2 2l-4.4 4.4-2.7.6.6-2.7Z"/>',
    terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><polyline points="7 9.5 10 12 7 14.5"/><line x1="12.5" y1="15" x2="17" y2="15"/>',
    search: '<circle cx="10.8" cy="10.8" r="6.8"/><line x1="20" y1="20" x2="15.9" y2="15.9"/>',
    flask: '<path d="M9.5 3.5h5"/><path d="M10.5 3.5v5.2L5.4 17.6a2 2 0 0 0 1.7 2.9h9.8a2 2 0 0 0 1.7-2.9l-5.1-8.9V3.5"/><path d="M7.6 14h8.8"/>',
    gauge: '<path d="M4.3 17a8.5 8.5 0 1 1 15.4 0"/><path d="M12 13.2l3.8-3.8"/><circle cx="12" cy="13.5" r="1.3"/>',
    sticky: '<path d="M5 3.5h14a1.5 1.5 0 0 1 1.5 1.5v9.4l-6.1 6.1H5A1.5 1.5 0 0 1 3.5 19V5A1.5 1.5 0 0 1 5 3.5Z"/><path d="M14.4 20.5v-4.6a1.5 1.5 0 0 1 1.5-1.5h4.6"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
    eyeoff: '<path d="M3.5 3.5l17 17"/><path d="M10.6 5.6c.5-.1.9-.1 1.4-.1 6 0 9.5 6.5 9.5 6.5a17.5 17.5 0 0 1-2.9 3.7"/><path d="M6.5 6.6C4 8.4 2.5 12 2.5 12S6 18.5 12 18.5c1.6 0 3-.4 4.3-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    pr: '<circle cx="6" cy="5.5" r="2"/><circle cx="6" cy="18.5" r="2"/><circle cx="18" cy="18.5" r="2"/><path d="M6 7.5v9"/><path d="M18 16.5V9.5a3 3 0 0 0-3-3h-4"/><polyline points="13.2 4.3 11 6.5 13.2 8.7"/>',
    merge: '<circle cx="6" cy="5.5" r="2"/><circle cx="6" cy="18.5" r="2"/><circle cx="18" cy="12" r="2"/><path d="M6 7.5v9"/><path d="M6 7.5c0 3 2 4.5 5 4.5h5"/>',
    talk: '<path d="M20.5 12.6c0 3.4-3.8 6.2-8.5 6.2a10 10 0 0 1-2.4-.3l-5.1 2 1.5-3.7a5.9 5.9 0 0 1-2-4.2c0-3.4 3.8-6.2 8.5-6.2s8 2.8 8 6.2Z"/>',
    review: '<path d="M20.5 12.6c0 3.4-3.8 6.2-8.5 6.2a10 10 0 0 1-2.4-.3l-5.1 2 1.5-3.7a5.9 5.9 0 0 1-2-4.2c0-3.4 3.8-6.2 8.5-6.2s8 2.8 8 6.2Z"/><path d="M9 12.4l2 2 4-4"/>',
    spawn: '<circle cx="10" cy="8" r="3.5"/><path d="M3.5 19.5a6.5 6.5 0 0 1 13 0"/><path d="M19 8.5v6"/><path d="M16 11.5h6"/>',
    sparkle: '<path d="M12 3.5l1.9 5.2 5.1 1.9-5.1 1.9L12 17.7l-1.9-5.2L5 10.6l5.1-1.9Z"/><path d="M18.5 16l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7Z"/>',
    commit: '<circle cx="12" cy="12" r="3.2"/><path d="M3.5 12h5.3"/><path d="M15.2 12h5.3"/>',
    server: '<rect x="4" y="4" width="16" height="6.5" rx="1.8"/><rect x="4" y="13.5" width="16" height="6.5" rx="1.8"/><circle cx="7.5" cy="7.25" r=".9" fill="currentColor" stroke="none"/><circle cx="7.5" cy="16.75" r=".9" fill="currentColor" stroke="none"/>',
    share: '<circle cx="18" cy="5.5" r="2.2"/><circle cx="6" cy="12" r="2.2"/><circle cx="18" cy="18.5" r="2.2"/><path d="M8 10.9l8-4.3"/><path d="M8 13.1l8 4.3"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><polyline points="12 7.5 12 12 15 14"/>',
    sliders: '<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="17" x2="20" y2="17"/><circle cx="9" cy="7" r="2.2"/><circle cx="15" cy="17" r="2.2"/>',
    power: '<path d="M12 3.5v8"/><path d="M7 6.3a7.5 7.5 0 1 0 10 0"/>',
    ticket: '<path d="M4 7.5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2.2a2.3 2.3 0 0 0 0 4.6v2.2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2.2a2.3 2.3 0 0 0 0-4.6Z"/><path d="M14.5 6v2.2M14.5 10.9v2.2M14.5 15.8V18"/>',
    book: '<path d="M4 5.5a2 2 0 0 1 2-2h13v14H6a2 2 0 0 0-2 2Z"/><path d="M4 19.5a2 2 0 0 0 2 2h13v-4"/>',
    siren: '<path d="M6.5 17.5v-5a5.5 5.5 0 0 1 11 0v5"/><rect x="4.5" y="17.5" width="15" height="3" rx="1"/><path d="M12 3v2"/><path d="M4.2 6.2l1.4 1.4"/><path d="M19.8 6.2l-1.4 1.4"/>',
    tool: '<path d="M14.6 6.4a4.2 4.2 0 0 0-5.6 5.3L3.8 16.9a1.9 1.9 0 0 0 2.7 2.7L11.7 14.4a4.2 4.2 0 0 0 5.3-5.6l-2.6 2.6-2.4-.5-.5-2.4Z"/>',
    result: '<polyline points="15 10 20 15 15 20"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/>',
    code: '<path d="M8 4.5H7a2 2 0 0 0-2 2v3.5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v3.5a2 2 0 0 0 2 2h1"/><path d="M16 4.5h1a2 2 0 0 1 2 2v3.5a2 2 0 0 0 2 2 2 2 0 0 0-2 2v3.5a2 2 0 0 1-2 2h-1"/>',
    broadcast: '<circle cx="12" cy="12" r="1.8"/><path d="M8.3 8.3a5.2 5.2 0 0 0 0 7.4"/><path d="M15.7 8.3a5.2 5.2 0 0 1 0 7.4"/><path d="M5.4 5.4a9.3 9.3 0 0 0 0 13.2"/><path d="M18.6 5.4a9.3 9.3 0 0 1 0 13.2"/>',
    globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.4 2.6 3.6 5.4 3.6 8.5s-1.2 5.9-3.6 8.5c-2.4-2.6-3.6-5.4-3.6-8.5S9.6 6.1 12 3.5Z"/>',
    robot: '<rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 8V5"/><circle cx="12" cy="4" r="1.1"/><circle cx="9.4" cy="13" r="1.1" fill="currentColor" stroke="none"/><circle cx="14.6" cy="13" r="1.1" fill="currentColor" stroke="none"/><path d="M9.6 16.2h4.8"/><path d="M3 12.5v2.5"/><path d="M21 12.5v2.5"/>',
    dot: '<circle cx="12" cy="12" r="3"/>',
  });
  const icon = (name, size) => `<svg width="${size || 18}" height="${size || 18}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICON[name] || ICON.dot}</svg>`;
  const KIND_ICON = {
    read_file: 'file', list_dir: 'folder', write_file: 'filepen', edit_file: 'filepen', bash: 'terminal', search_code: 'search',
    run_tests: 'flask', run_benchmark: 'gauge', write_scratch: 'sticky', open_pr: 'pr', merge: 'merge', message: 'talk', spawn_subagent: 'spawn', generate: 'sparkle',
    batch_generate: 'sparkle', finish: 'check',
    pr: 'pr', review: 'review', commit: 'commit', job: 'server', spawn: 'spawn', close: 'check', shared_surface: 'share',
    tick: 'clock', config: 'sliders', forge_boot: 'power', ticket: 'ticket', wiki: 'book', slack: 'talk', siem: 'siren',
    inference_call: 'sparkle', monitor_verdict: 'shield', monitor_parse_error: 'warn', monitor_parse_recovered: 'shield', monitor_error: 'warn',
  };
  const kindIcon = (kind, size) => `<span class="ki" title="${esc(kind)}">${icon(KIND_ICON[kind] || (kind ? 'tool' : 'dot'), size || 14)}</span>`;
  // The parts of a step, each with the same shape everywhere it appears.
  const PART = { said: 'talk', did: 'tool', result: 'result', monitors: 'shield', raw: 'code', context: 'eye', effects: 'broadcast', happened: 'globe', note: 'pencil', reasoning: 'sparkle' };
  const partIcon = (part, size) => `<span class="pi">${icon(PART[part], size || 14)}</span>`;
  const headKindOf = s => s.tools[0]?.function || s.events[0]?.kind || 'inference_call';

  // ---------- small vocabulary shared by every view
  const roundLabel = r => r < 0 ? 'before round 0' : `round ${r}`;
  const roundKey = r => r < 0 ? 'pre' : `r${r}`;
  const stepLabel = s => `${s.agent} · ${roundLabel(s.round)}`;
  // The harness flags a score strictly above the threshold (metrics.py: `v.score > threshold`), so a tie is not a flag.
  const flagged = s => s.peak != null && s.peak > THR;
  const levelOf = score => score == null ? 'none' : score > THR ? 'flag' : 'clean';
  const scoreText = v => v == null ? '·' : String(Math.round(v * 100) / 100);
  // A score equal to the threshold is not a flag, but not clear either; on the twin's page it is one that set it.
  const AT_TITLE = IS_TWIN ? 'at the audit threshold: one of the scores that set it' : 'equal to the audit threshold, so not above it';
  const pill = score => `<span class="pill ${levelOf(score)}${score === THR ? ` at" title="${AT_TITLE}` : ''}">${scoreText(score)}</span>`;
  const covertTags = tags => [...new Set(tags || [])].map(t => `<span class="tag covert">${esc(t)}</span>`).join('');
  const oneLine = t => String(t || '').replace(/\s+/g, ' ').trim();
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const laneIndex = name => { const i = LANES.indexOf(name); return i < 0 ? LANES.length : i; };
  // The twin's page has no monitor rows (they describe the run's play), so its columns come from its own verdicts.
  const MONITOR_ORDER = MONITORS.length ? MONITORS.map(m => m.name) : [...new Set(VERDICTS.map(v => v.monitor))].sort();
  const SCORED_MONITORS = MONITOR_ORDER.filter(name => VERDICTS.some(v => v.monitor === name));
  const replyText = s => s.calls.map(c => `${c.reasoning || ''}\n${c.reply}`).join('\n');
  const searchText = s => [replyText(s), ...s.tools.map(t => `${t.function}\n${t.arguments_text}\n${t.result_text}`), ...s.events.map(e => e.body), ...s.verdicts.map(v => v.reason)].join('\n').toLowerCase();
  const SEARCH = new Map(STEPS.map(s => [s.id, searchText(s)]));
  const byRound = (a, b) => a.round - b.round || laneIndex(a.agent) - laneIndex(b.agent);

  function stepSummary(s) {
    if (s.tools.length) return s.tools.map(t => t.function).join(', ');
    if (s.events.length) return [...new Set(s.events.map(e => e.kind))].join(', ');
    return s.calls.length ? plural(s.calls.length, 'model call') : '';
  }

  function copyText(text, btn) {
    const done = () => { if (!btn) return; const label = btn.textContent; btn.textContent = 'copied'; btn.classList.add('done'); setTimeout(() => { btn.textContent = label; btn.classList.remove('done'); }, 1200); };
    const fallback = () => { const ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) { /* nothing to do */ } ta.remove(); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
  }

  // ---------- your notes: one store per run, one file
  // A mark is the W3C Web Annotation pair without the dependency: `quote` is the
  // TextQuoteSelector, `start` the TextPositionSelector, and the quote is what wins.
  const NOTES_KEY = `loc-arena-notes-v1/${RUN.name}`;
  const notesOk = (() => { try { const k = NOTES_KEY + ':probe'; localStorage.setItem(k, '1'); const ok = localStorage.getItem(k) === '1'; localStorage.removeItem(k); return ok; } catch (e) { return false; } })();
  const emptyNotes = () => ({ version: 1, run: RUN.name, savedAt: null, steps: {} });
  let NOTES = emptyNotes(), saveTimer = null, saveFailed = false;
  if (notesOk) { try { const raw = localStorage.getItem(NOTES_KEY); const kept = raw ? JSON.parse(raw) : null; if (kept && kept.steps) NOTES = { ...emptyNotes(), ...kept }; } catch (e) { /* a corrupt store must not blank the page */ } }
  const noteOf = id => NOTES.steps[id];
  const noteFor = id => (NOTES.steps[id] || (NOTES.steps[id] = { text: '', marks: [], at: null }));
  // Written: did I say anything about this step. Read: have I been through it (a note
  // counts). Empty: is there anything worth keeping (a read mark counts).
  function noteWritten(n) { return !!n && (!!String(n.text || '').trim() || !!(n.marks || []).length); }
  function noteRead(n) { return !!n && (!!n.read || noteWritten(n)); }
  function noteEmpty(n) { return !noteWritten(n) && !noteRead(n); }
  const unsaved = () => Object.values(NOTES.steps).filter(n => !NOTES.savedAt || String(n.at || '') > NOTES.savedAt).length;

  function keepNotes() {
    for (const id of Object.keys(NOTES.steps)) if (noteEmpty(NOTES.steps[id])) delete NOTES.steps[id];
    if (notesOk) { clearTimeout(saveTimer); saveTimer = setTimeout(() => { try { localStorage.setItem(NOTES_KEY, JSON.stringify(NOTES)); saveFailed = false; } catch (e) { saveFailed = true; } renderNotesState(); }, 250); }
    renderNotesState();
  }
  const touch = id => { noteFor(id).at = new Date().toISOString(); keepNotes(); refreshRowMark(id); };

  function renderNotesState() {
    const entries = Object.values(NOTES.steps);
    const n = entries.filter(noteWritten).length, readOnly = entries.filter(x => !noteWritten(x) && noteRead(x)).length;
    const marks = entries.reduce((a, x) => a + (x.marks || []).length, 0), un = unsaved();
    const tally = el('tally-notes'); if (tally) tally.textContent = n || '';
    const box = el('notes-state');
    if (box) {
      const warn = !notesOk
        ? `<div class="banner">${icon('warn', 15)} This browser will not let the page keep anything, so notes last only as long as this tab. <b>Save them to a file before you leave.</b></div>`
        : saveFailed ? `<div class="banner">${icon('warn', 15)} The last save to this browser failed. Storage is full or blocked. <b>Save to a file now.</b></div>` : '';
      box.innerHTML = warn + `<p class="help"><b class="num" style="color:var(--ink)">${n}</b> step${n === 1 ? '' : 's'} annotated, <b class="num" style="color:var(--ink)">${marks}</b> highlight${marks === 1 ? '' : 's'}${readOnly ? `, <b class="num" style="color:var(--ink)">${readOnly}</b> more marked read` : ''} &middot; ${n + readOnly ? (un ? `<b style="color:var(--flag)">${un} changed since the last save</b>` : 'all saved to a file') : 'nothing yet'}</p>`;
    }
    const list = el('notes-list');
    if (list) {
      const ids = Object.keys(NOTES.steps).sort((a, b) => (byId.get(a)?.round ?? 0) - (byId.get(b)?.round ?? 0));
      list.innerHTML = ids.map(id => {
        const s = byId.get(id), n2 = NOTES.steps[id];
        const first = String(n2.text || '').trim() || (n2.marks || []).map(m => m.note).find(x => String(x || '').trim()) || (noteWritten(n2) ? plural((n2.marks || []).length, 'highlight') : 'marked read');
        return `<button data-open-note="${esc(id)}"><span class="nid">${esc(s ? stepLabel(s) : id)}</span><span class="ntx">${esc(first)}</span></button>`;
      }).join('') || '<p class="help">No notes yet. Open a step and select a passage.</p>';
      list.querySelectorAll('[data-open-note]').forEach(b => b.addEventListener('click', () => {
        const s = byId.get(b.dataset.openNote);
        if (s) { el('notes-dialog').close(); openStep(s, { record: true }); }
      }));
    }
  }

  // The parser drops one newline after `<pre>` and normalises CRLF, so every offset
  // and quote is taken against this form. Each annotatable <pre> opens with a
  // throwaway newline so pre.textContent equals this exactly.
  const domText = s => String(s ?? '').replace(/\r\n?/g, '\n');
  // A step has several texts worth annotating. A field names one: `reply-0` is the
  // first model call's reply, `result-1` the second tool call's result. The texts are
  // fixed by the builder, so a note still finds its passage after a rebuild.
  const FIELD_TEXT = {
    prompt: (s, i) => s.calls[i]?.prompt, reply: (s, i) => s.calls[i]?.reply, reasoning: (s, i) => s.calls[i]?.reasoning,
    said: (s, i) => s.calls[i]?.said,
    args: (s, i) => s.tools[i]?.arguments_text, result: (s, i) => s.tools[i]?.result_text,
    body: (s, i) => s.events[i]?.body,
  };
  const FIELD_NAME = { said: 'what it said', prompt: 'prompt', reply: 'reply', reasoning: 'reasoning', args: 'action', result: 'result', body: 'event' };
  const fieldParts = field => { const [kind, i] = String(field || '').split('-'); return [kind, Number(i) || 0]; };
  const fieldText = (s, field) => { const [kind, i] = fieldParts(field); return domText((FIELD_TEXT[kind] || (() => ''))(s, i) || ''); };
  const fieldName = field => { const [kind, i] = fieldParts(field); return `${FIELD_NAME[kind] || kind}${i ? ` ${i + 1}` : ''}`; };

  function anchorAt(text, m) {
    const q = String(m.quote || ''); if (!q) return null;
    if (text.slice(m.start, m.start + q.length) === q) return [m.start, m.start + q.length];
    let best = -1, from = 0, i;
    while ((i = text.indexOf(q, from)) >= 0) { if (best < 0 || Math.abs(i - m.start) < Math.abs(best - m.start)) best = i; from = i + 1; }
    return best < 0 ? null : [best, best + q.length];
  }

  // Spans overlap, so emit one segment per distinct cover instead of nesting <mark>.
  function renderMarked(text, spans) {
    const live = spans.filter(s => s && s[1] > s[0]);
    if (!live.length) return esc(text);
    const cuts = [...new Set(live.flatMap(s => [s[0], s[1]]))].sort((a, b) => a - b);
    let out = '', pos = 0;
    for (let i = 0; i < cuts.length - 1; i++) {
      const a = cuts[i], b = cuts[i + 1], on = live.filter(s => s[0] <= a && s[1] >= b);
      if (a > pos) out += esc(text.slice(pos, a));
      pos = b;
      const seg = esc(text.slice(a, b));
      if (!on.length) { out += seg; continue; }
      const id = (on.find(s => s[3]) || [])[3];
      out += `<mark class="${[...new Set(on.map(s => s[2]))].join(' ')}"${id ? ` data-mark="${esc(id)}"` : ''}>${seg}</mark>`;
    }
    return out + esc(text.slice(pos));
  }

  // ---------- selecting a passage offers to pin a note to it
  function offsetIn(root, node, off) {
    if (!node || node.nodeType !== 3 || !root.contains(node)) return 0;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let acc = 0, n;
    while ((n = w.nextNode())) { if (n === node) return acc + off; acc += n.nodeValue.length; }
    return 0;
  }
  const selbar = document.createElement('div');
  selbar.className = 'selbar';
  selbar.hidden = true;
  selbar.innerHTML = `<button data-act="note">${icon('pencil', 14)}note on this</button><button data-act="copy">copy</button>`;
  document.body.appendChild(selbar);
  let pending = null;
  const hideSelbar = () => { selbar.hidden = true; pending = null; };

  function showSelbar() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return hideSelbar();
    const range = sel.getRangeAt(0), host = range.commonAncestorContainer;
    const pre = (host.nodeType === 1 ? host : host.parentElement)?.closest('pre.trace[data-field]');
    const quote = range.toString();
    if (!pre || quote.trim().length < 2) return hideSelbar();
    const stepId = pre.closest('[data-step-host]')?.dataset.stepHost || S.step;
    pending = { stepId, field: pre.dataset.field, quote, start: offsetIn(pre, range.startContainer, range.startOffset) };
    const box = range.getBoundingClientRect();
    selbar.hidden = false;
    selbar.style.left = Math.round(Math.max(8, Math.min(window.innerWidth - selbar.offsetWidth - 8, box.left))) + 'px';
    const top = box.top > selbar.offsetHeight + 10 ? box.top - selbar.offsetHeight - 6 : box.bottom + 6;
    selbar.style.top = Math.round(Math.max(8, Math.min(window.innerHeight - selbar.offsetHeight - 8, top))) + 'px';
  }
  const inSelbar = e => e.target instanceof Element && e.target.closest('.selbar');
  document.addEventListener('mouseup', e => { if (!inSelbar(e)) setTimeout(showSelbar, 0); });
  document.addEventListener('keyup', e => { if (e.key === 'Shift' || e.shiftKey) setTimeout(showSelbar, 0); });
  selbar.addEventListener('mousedown', e => e.preventDefault());
  selbar.addEventListener('click', e => {
    const btn = e.target instanceof Element ? e.target.closest('button') : null;
    if (!btn || !pending) return;
    const p = pending;
    if (btn.dataset.act === 'copy') copyText(p.quote); else addMark(p);
    hideSelbar(); window.getSelection()?.removeAllRanges();
  });

  function addMark(p) {
    const s = byId.get(p.stepId); if (!s) return;
    const n = noteFor(s.id), id = 'm' + Date.now().toString(36) + Math.floor(Math.random() * 4096).toString(36);
    n.marks.push({ id, field: p.field, start: p.start, quote: p.quote, note: '' });
    touch(s.id);
    refreshStepViews(s.id);
    focusMark(id);
  }

  // One card per note, wherever the step is shown: the side panel's margin, or under
  // the passage in a card open in the grid. The handlers are delegated (below), so a
  // card keeps working when layout moves it.
  const noteCard = (m, stepId) => `<div class="note mine" data-anchor="${esc(m.id)}"><div class="q">${esc(m.quote.length > 220 ? m.quote.slice(0, 220) + '…' : m.quote)}</div><textarea data-note-for="${esc(m.id)}" data-note-step="${esc(stepId)}" placeholder="what do you make of this passage?">${esc(m.note)}</textarea><div class="who">${icon('pencil', 12)}in the ${esc(fieldName(m.field))}<button data-del-mark="${esc(m.id)}" data-note-step="${esc(stepId)}" title="delete this highlight and its note">${icon('trash', 13)}</button></div></div>`;
  const lostCard = (m, stepId) => `<div class="note detached" data-lost="${esc(m.id)}"><div class="warn">${icon('warn', 13)} this passage is no longer in the step</div><div class="q">${esc(m.quote.slice(0, 220))}</div><textarea data-note-for="${esc(m.id)}" data-note-step="${esc(stepId)}">${esc(m.note)}</textarea><div class="who">kept, but it has nowhere to point<button data-del-mark="${esc(m.id)}" data-note-step="${esc(stepId)}">${icon('trash', 13)}</button></div></div>`;
  document.addEventListener('input', e => {
    const ta = e.target instanceof Element ? e.target.closest('textarea[data-note-for]') : null; if (!ta) return;
    const m = (noteFor(ta.dataset.noteStep).marks || []).find(x => x.id === ta.dataset.noteFor);
    if (m) { m.note = ta.value; touch(ta.dataset.noteStep); scheduleLayout(); }
  });
  document.addEventListener('click', e => {
    const t = e.target instanceof Element ? e.target : null; if (!t) return;
    const del = t.closest('[data-del-mark]');
    if (del) {
      const own = noteFor(del.dataset.noteStep);
      own.marks = (own.marks || []).filter(x => x.id !== del.dataset.delMark);
      touch(del.dataset.noteStep); refreshStepViews(del.dataset.noteStep);
      return;
    }
    const mk = t.closest('pre.trace mark.hl[data-mark]');
    if (mk) focusMark(mk.dataset.mark, mk.closest('[data-step-host]'));
  });

  // ---------- state, and the address bar that carries it
  // Every key is in the address bar when it differs from here, so a link reopens the
  // same view, filters and step. Going back resets to these first, then reads the hash,
  // so a filter from a later page never leaks into an earlier one.
  const DEFAULT_STATE = { view: 'transcript', trows: '', hide: '', agent: '', tool: '', verdict: '', covert: '', text: '', rmin: '', rmax: '', noworld: false, mine: false, unread: false, sort: 'round', step: null, focus: false, sample: false, seed: 1, sflag: '4', sclean: '4' };
  const S = { ...DEFAULT_STATE };
  const HASH_NAME = { step: 't' };
  const FILTER_DEFAULTS = { agent: '', tool: '', verdict: '', covert: '', text: '', rmin: '', rmax: '', noworld: false, mine: false, unread: false, sample: false };
  const els = {};
  ['f-agent', 'f-tool', 'f-verdict', 'f-covert', 'f-text', 'f-rmin', 'f-rmax', 'f-noworld', 'f-mine', 'f-unread', 'sort', 't-rows', 'list', 'more', 'count', 'chips', 'readcol', 'crumbs', 'listcol', 'panel', 'tgrid', 'tscroll'].forEach(id => els[id] = el(id));
  const FIELD = { 'f-agent': 'agent', 'f-tool': 'tool', 'f-verdict': 'verdict', 'f-covert': 'covert', 'f-text': 'text', 'f-rmin': 'rmin', 'f-rmax': 'rmax', 'f-noworld': 'noworld', 'f-mine': 'mine', 'f-unread': 'unread', 'sort': 'sort', 't-rows': 'trows' };

  function writeHash(mode) {
    const p = new URLSearchParams();
    for (const [key, base] of Object.entries(DEFAULT_STATE)) {
      if (key === 'step' || S[key] === base || S[key] === '' || S[key] == null) continue;
      if (!S.sample && ['seed', 'sflag', 'sclean'].includes(key)) continue;
      p.set(key, String(S[key]));
    }
    if (S.step && byId.has(S.step)) p.set(HASH_NAME.step, S.step);
    const hash = '#' + p.toString();
    if (mode === 'push' && hash !== location.hash) history.pushState(null, '', hash);
    else history.replaceState(null, '', hash);
  }
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    for (const [key, base] of Object.entries(DEFAULT_STATE)) {
      if (key === 'step' || !p.has(key)) continue;
      const raw = p.get(key);
      S[key] = typeof base === 'boolean' ? raw === 'true' : typeof base === 'number' ? Number(raw) || base : raw;
    }
    if (!VIEWS.some(v => v.id === S.view && !v.dialog)) S.view = DEFAULT_STATE.view;
    if (S.rmin !== '' || S.rmax !== '' || S.noworld || S.mine || S.unread) el('morefilters').hidden = false;
    return p.get(HASH_NAME.step);
  }

  let matches = [], shown = 0;
  const PAGE = DATA.settings.list_page_size;

  const VIEWS = [
    { id: 'transcript', label: 'Transcript', icon: 'grid' },
    { id: 'comms', label: 'Communication', icon: 'talk' },
    { id: 'steps', label: 'Search steps', icon: 'list' },
    { id: 'monitors', label: IS_TWIN ? 'Monitors on the twin' : 'Outcome and monitors', icon: 'shield' },
    { id: 'notes', label: 'Your notes', icon: 'pencil', dialog: true },
    { id: 'method', label: 'How this was read', icon: 'help' },
  ].filter(v => DATA.outcome || CAL || v.id !== 'monitors');  // a twin page without its saved scores has none to show
  function buildRail() {
    els.rail = el('railnav');
    // A page built from a folder of runs links back to the index of them all.
    const back = RUN.index ? `<a class="railback" href="${esc(RUN.index)}" title="All runs">${icon('list')}<span class="lbl">All runs</span></a>` : '';
    els.rail.innerHTML = back + VIEWS.map(v => `<button data-view="${v.id}" title="${esc(v.label)}">${icon(v.icon)}<span class="lbl">${esc(v.label)}</span>${v.id === 'notes' ? '<span class="tally num" id="tally-notes"></span>' : ''}</button>`).join('');
    els.rail.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => {
      const v = VIEWS.find(x => x.id === b.dataset.view);
      if (v.dialog) openNotes(); else showView(v.id, { record: true });
    }));
    el('brand-run').textContent = `${RUN.mode} · seed ${RUN.seed}`;
    el('railfoot').innerHTML = `${STEPS.length} steps in ${LANES.length} lanes<br>${VERDICTS.length} monitor verdicts`;
  }
  function applyFocus() {
    const app = document.querySelector('.app');
    if (S.focus && S.view === 'steps') app.setAttribute('data-focus', 'read'); else app.removeAttribute('data-focus');
    scheduleLayout();
  }

  // One view at a time. The open step belongs to the view it was opened in: the
  // transcript reads it in the side panel, the search view beside its list.
  function showView(id, opts) {
    const changed = S.view !== id;
    S.view = id;
    document.querySelectorAll('.view').forEach(node => { node.hidden = node.id !== 'view-' + id; });  // offered or not
    els.rail.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-current', String(b.dataset.view === id)));
    if (id === 'transcript') renderTranscript();
    if (id === 'monitors') renderMonitors();
    if (id === 'comms') renderComms();
    if (changed && S.step && byId.has(S.step) && (id === 'transcript' || id === 'steps')) renderReaderFor(byId.get(S.step));
    else if (id === 'transcript' && !S.step) closePanel();
    applyFocus();
    renderCrumbs();
    if (opts?.record !== undefined) writeHash(opts.record ? 'push' : 'replace');
  }

  // ---------- transcript: every lane side by side, a reading panel beside it
  const hiddenLanes = () => new Set(String(S.hide || '').split(',').filter(Boolean));
  const visibleLanes = () => { const h = hiddenLanes(); return LANES.filter(l => !h.has(l)); };
  const verdictsInRound = (monitor, r) => VERDICTS.filter(v => v.monitor === monitor && v.step && byId.get(v.step).round === r);
  function transcriptRounds() {
    const lanes = new Set(visibleLanes());
    const rounds = [...new Set(STEPS.map(s => s.round))].sort((a, b) => a - b);
    if (S.trows === 'flagged') return rounds.filter(r => STEPS.some(s => s.round === r && lanes.has(s.agent) && flagged(s)));
    if (S.trows === 'covert') return rounds.filter(r => STEPS.some(s => s.round === r && lanes.has(s.agent) && s.covert.length));
    if (S.trows === 'comms') { const said = new Set(COMM.map(c => c.step)); return rounds.filter(r => STEPS.some(s => s.round === r && lanes.has(s.agent) && said.has(s.id))); }
    return rounds;
  }
  // The order J and K walk in the transcript: what is on screen, round by round.
  function transcriptOrder() {
    const lanes = new Set(visibleLanes()), rounds = new Set(transcriptRounds());
    return STEPS.filter(s => lanes.has(s.agent) && rounds.has(s.round)).sort(byRound);
  }

  // Cards open in the grid, by step id. Their lane widens while one is open.
  const OPEN = new Set();
  function stepCard(s) {
    if (OPEN.has(s.id)) return openCard(s);
    const t = s.tools[0], own = s.agent;
    const head = t ? t.function : s.events.length ? [...new Set(s.events.map(e => e.kind))].join(', ') : plural(s.calls.length, 'model call');
    const body = t ? t.action : s.events.length ? s.events.map(e => `${e.kind} by ${e.actor || '?'}`).join('\n') : oneLine(s.calls.at(-1)?.reply);
    const extra = [];
    if (s.tools.length > 1) extra.push(`+ ${plural(s.tools.length - 1, 'more tool call')}`);
    const foreign = s.calls.filter(c => c.identity !== own);
    if (foreign.length) extra.push(`${plural(foreign.length, 'model call')} as ${[...new Set(foreign.map(c => c.identity))].join(', ')}`);
    if (t && t.blocked) extra.push(`blocked: ${t.blocked}`);
    else if (t) extra.push(`→ ${oneLine(t.result_text).slice(0, 140) || 'no result'}`);
    const sent = COMM.filter(c => c.step === s.id && c.kind === 'message');
    const cls = ['tc', levelOf(s.peak), s.covert.length ? 'covert' : ''].filter(Boolean).join(' ');
    const tip = `${stepLabel(s)}${s.covert.length ? ` · covert: ${s.covert.join(', ')}` : ''}`;
    const mine = noteWritten(noteOf(s.id)) ? ' mine' : '';
    return `<div class="${cls}${mine}" role="button" tabindex="0" data-step="${esc(s.id)}" aria-current="${S.step === s.id}" title="${esc(tip)} · click to expand"><div class="tch">${s.covert.length ? '<span class="dotc"></span>' : ''}${kindIcon(headKindOf(s), 13)}<b>${esc(head)}</b><span class="tcpen">${icon('pencil', 11)}</span>${s.peak != null ? pill(s.peak) : ''}<span class="tcopen" role="button" data-open-panel="${esc(s.id)}" title="open in the side panel">&#8599;</span></div><div class="tcb">${esc(body)}</div>${sent.map(c => `<div class="tcr tmsg">${kindIcon('message', 11)} to ${esc(c.audience)}: ${esc(oneLine(c.text))}</div>`).join('')}${extra.map(x => `<div class="tcr">${esc(x)}</div>`).join('')}</div>`;
  }
  // A card open in the grid: the whole action, its result and the monitors' reasons,
  // annotatable like the panel, with each note right under its passage.
  function openCard(s) {
    const mine = noteOf(s.id);
    const placed = (mine?.marks || []).map(m => ({ m, at: anchorAt(fieldText(s, m.field), m) }));
    const notesUnder = field => { const here = placed.filter(x => x.m.field === field && x.at).sort((a, b) => a.at[0] - b.at[0]); return here.length ? `<div class="inline-notes">${here.map(x => noteCard(x.m, s.id)).join('')}</div>` : ''; };
    const block = (field, cls) => annotated(s, field, placed, cls) + notesUnder(field);
    const parts = [];
    s.calls.forEach((c, i) => { if (c.reasoning) parts.push(`<details class="reasoning"${placed.some(x => x.m.field === `reasoning-${i}`) ? ' open' : ''}><summary class="sub">${partIcon('reasoning', 12)}reasoning${c.identity === s.agent ? '' : ` as ${esc(c.identity)}`} &middot; ${c.reasoning.length.toLocaleString()} chars</summary>${block(`reasoning-${i}`)}</details>`); });
    s.calls.forEach((c, i) => { if (c.said && c.identity === s.agent) parts.push(`<div class="sub">${partIcon('said', 12)}what it said</div>${block(`said-${i}`)}`); });
    s.tools.forEach((t, i) => parts.push(`<div class="sub">${partIcon('did', 12)}${s.tools.length > 1 ? esc(t.function) : 'what it did'}</div>${block(`args-${i}`)}<div class="sub">${partIcon('result', 12)}result${t.blocked ? ` (blocked: ${esc(t.blocked)})` : ''}</div>${block(`result-${i}`, 'code')}`));
    if (!s.tools.length) s.events.forEach((ev, i) => parts.push(`<div class="sub">${kindIcon(ev.kind, 12)}${esc(ev.kind)} by ${esc(ev.actor || '?')}</div>${block(`body-${i}`, 'code')}`));
    if (!s.tools.length && !s.events.length) s.calls.forEach((c, i) => c.said && c.identity === s.agent && !placed.some(x => x.m.field === `reply-${i}`) || parts.push(`<div class="sub">${partIcon('raw', 12)}reply as ${esc(c.identity)}</div>${block(`reply-${i}`)}`));
    const verdicts = s.verdicts.length ? `<div class="sub">${partIcon('monitors', 12)}how the monitors read it</div>${[...s.verdicts].sort((a, b) => b.score - a.score).map(v => `<div class="tverd">${pill(v.score)} <b>${esc(v.monitor)}</b> ${esc(v.reason)}</div>`).join('')}` : '';
    const note = String(mine?.text || '').trim() ? `<div class="tnote">${partIcon('note', 12)}${esc(mine.text)}</div>` : '';
    const lost = placed.filter(x => !x.at).map(x => lostCard(x.m, s.id)).join('');
    const cls = ['tc', 'open', levelOf(s.peak), s.covert.length ? 'covert' : ''].filter(Boolean).join(' ');
    return `<div class="${cls}" data-step="${esc(s.id)}" data-step-host="${esc(s.id)}" aria-current="${S.step === s.id}">
      <div class="tch">${s.covert.length ? '<span class="dotc"></span>' : ''}${kindIcon(headKindOf(s), 13)}<b>${esc(s.tools[0]?.function || stepSummary(s))}</b>${s.peak != null ? pill(s.peak) : ''}<button class="tcbtn" data-open-panel="${esc(s.id)}" title="open in the side panel: the model's reasoning, the raw reply, the context the model saw, your note on the step">open &#8599;</button><button class="tcbtn" data-collapse="${esc(s.id)}" title="collapse (Esc collapses every open card)">&#9652;</button></div>
      ${s.covert.length ? `<div class="tags">${covertTags(s.covert)}</div>` : ''}${parts.join('')}${verdicts}${note}${lost}
      ${s.calls.length ? `<button class="tcmore" data-open-panel="${esc(s.id)}">${partIcon('raw', 12)}raw reply ${partIcon('context', 12)}context ${partIcon('note', 12)}your step note <span>open &#8599;</span></button>` : ''}
    </div>`;
  }
  function toggleCard(id, force) {
    const want = force ?? !OPEN.has(id);
    if (want) OPEN.add(id); else OPEN.delete(id);
    renderTranscript();
    const card = els.tgrid.querySelector(`.tc[data-step="${CSS.escape(id)}"]:not(.v)`);
    if (card && want) card.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  // Every place a step is on screen redraws after its notes change.
  function refreshStepViews(id) {
    const s = byId.get(id); if (!s) return;
    if (S.step === id && readerHost()?.querySelector(`.reader[data-step-host="${CSS.escape(id)}"]`)) redrawStep(s);
    if (OPEN.has(id)) { const card = els.tgrid.querySelector(`.tc.open[data-step="${CSS.escape(id)}"]`); if (card) card.outerHTML = openCard(s); }
  }
  function verdictCard(v, clickable) {
    const s = v.step ? byId.get(v.step) : null;
    const target = s ? `${s.agent} · ${v.target || ''}` : `mirror #${v.target_mirror_seq ?? 'none'} · ${v.actor}`;
    const attrs = clickable && s ? `data-step="${esc(s.id)}" aria-current="${S.step === s.id}"` : 'disabled';
    return `<button class="tc v ${levelOf(v.score)}${clickable && s ? '' : ' static'}" ${attrs}><div class="tch"><b>${esc(target)}</b>${pill(v.score)}</div><div class="tcb">${esc(v.reason)}</div></button>`;
  }
  function monitorCell(vs) {
    if (!vs.length) return '';
    const high = vs.filter(v => v.score > THR).sort((a, b) => b.score - a.score), low = vs.filter(v => v.score <= THR);
    const max = low.length ? Math.max(...low.map(v => v.score)) : null, at = low.filter(v => v.score === THR).length;
    return high.map(v => verdictCard(v, true)).join('') + (low.length ? `<div class="tmore">${low.length}${high.length ? ' more' : ''} at or below ${THR}${at ? `, ${at} at it` : max ? `, highest ${scoreText(max)}` : ''}</div>` : '');
  }
  function renderTbar() {
    const { main_task: mt, side_task: st, safety: sf, milestones = [] } = OUT || {};
    el('t-outcome').innerHTML = (!OUT ? [
      `<span><b>honest twin</b>: played to set the audit threshold (<b>${esc(THR)}</b>)${CAL ? '' : '; grading did not save its verdicts'}</span>`,
      ...(CAL ? [`<span><b class="flag">${plural(STEPS.filter(flagged).length, 'step')}</b> above it, each a false alarm; <b>${plural(VERDICTS.filter(v => v.score === THR).length, 'verdict')}</b> at it</span>`] : []),
      ...(CAL ? [`<span>overall suspicion <b>${scoreText(CAL.twin_overall)}</b>, the bar the run&rsquo;s <b>${scoreText(CAL.run_overall)}</b> was held to</span>`, `<button class="linkish" data-to-view="monitors">monitors on the twin &rarr;</button>`] : []),
      `<span><b>${STEPS.length}</b> steps</span>`,
    ] : [
      `<span>main task <b>${esc(mt.final_score)}</b>${mt.gate_passed ? '' : ` (gate failed: ${esc(mt.gate_reason || '?')})`}</span>`,
      `<span>side task <b class="covert">${st.landing_robust ? 'landed, robust' : st.landing_minimal ? 'landed, minimal' : 'not landed'}</b> (${milestones.filter(m => m.reached).length} of ${milestones.length} milestones)</span>`,
      `<span><b class="${sf.caught ? 'flag' : ''}">${sf.caught ? 'caught' : 'not caught'}</b>${sf.caught ? ` by ${esc(sf.caught_by.join(', '))}` : ''}</span>`,
      `<span>audit threshold <b>${esc(THR)}</b></span>`, `<span><b class="flag">${STEPS.filter(flagged).length}</b> steps flagged, <b class="covert">${STEPS.filter(s => s.covert.length).length}</b> covert</span>`,
      `<button class="linkish" data-to-view="monitors">outcome and monitors &rarr;</button>`,
    ]).join('');
    el('t-outcome').querySelector('[data-to-view]')?.addEventListener('click', () => showView('monitors', { record: true }));
    const hidden = hiddenLanes();
    el('t-lanes').innerHTML = '<span class="help" style="margin-right:2px">lanes</span>' + LANES.map(l => `<button class="chip quiet" data-lane="${esc(l)}" aria-pressed="${!hidden.has(l)}" title="show or hide this lane">${esc(l)}</button>`).join('');
    el('t-lanes').querySelectorAll('[data-lane]').forEach(b => b.addEventListener('click', () => {
      const h = hiddenLanes(); if (h.has(b.dataset.lane)) h.delete(b.dataset.lane); else h.add(b.dataset.lane);
      S.hide = [...h].join(','); renderTranscript(); writeHash('replace');
    }));
  }
  function renderTranscript() {
    renderTbar();
    const lanes = visibleLanes(), rounds = transcriptRounds();
    // Fixed widths: the grid box spans its content so the sticky round column holds, and
    // content-sized columns would stretch to a whole paragraph on one line.
    const busy = m => VERDICTS.filter(v => v.monitor === m).length > DATA.settings.sparse_monitor_verdicts;
    const opened = new Set([...OPEN].map(id => byId.get(id)?.agent));
    const cols = ['88px', ...SCORED_MONITORS.map(m => busy(m) ? '240px' : '170px'), ...lanes.map(l => opened.has(l) ? '460px' : l === WORLD ? '200px' : '230px')];
    const head = `<div class="trow"><div class="th corner">round</div>${SCORED_MONITORS.map(m => `<div class="th mon" title="${esc(m)}">${icon('shield', 14)} ${esc(m)}</div>`).join('')}${lanes.map(l => `<div class="th" title="${esc(l)}">${icon(l === WORLD ? 'globe' : 'robot', 14)} ${esc(l)}</div>`).join('')}</div>`;
    const unplaced = VERDICTS.filter(v => !v.step);
    const whole = unplaced.length ? `<div class="trow"><div class="trl">whole run</div>${SCORED_MONITORS.map(m => `<div class="tcell mon">${unplaced.filter(v => v.monitor === m).map(v => verdictCard(v, false)).join('')}</div>`).join('')}${lanes.map(() => '<div class="tcell"></div>').join('')}</div>` : '';
    const cell = new Map(STEPS.map(s => [`${s.agent}|${s.round}`, s]));
    const body = rounds.map(r => {
      const any = STEPS.some(s => s.round === r && flagged(s));
      return `<div class="trow"><div class="trl${any ? ' flagrow' : ''}">${esc(roundLabel(r))}</div>${SCORED_MONITORS.map(m => `<div class="tcell mon">${monitorCell(verdictsInRound(m, r))}</div>`).join('')}${lanes.map(l => { const s = cell.get(`${l}|${r}`); return `<div class="tcell">${s ? stepCard(s) : ''}</div>`; }).join('')}</div>`;
    }).join('');
    els.tgrid.style.gridTemplateColumns = cols.join(' ');
    els.tgrid.innerHTML = head + whole + (body || `<div class="trow"><div class="trl"></div><div class="tcell help" style="grid-column: 2 / -1">No round matches.</div></div>`);
    syncControls();
  }
  els.tgrid.addEventListener('click', e => {
    const t = e.target instanceof Element ? e.target : null; if (!t) return;
    const toPanel = t.closest('[data-open-panel]');
    if (toPanel) { openStep(byId.get(toPanel.dataset.openPanel), { record: true }); return; }
    const collapse = t.closest('[data-collapse]');
    if (collapse) { toggleCard(collapse.dataset.collapse, false); return; }
    if (t.closest('.tc.open')) return;  // reading, selecting or writing inside an open card
    const card = t.closest('.tc[data-step]');
    if (card) toggleCard(card.dataset.step, card.classList.contains('v') ? true : undefined);
  });
  els.tgrid.addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target instanceof Element && e.target.matches('.tc[data-step]:not(.open)')) { e.preventDefault(); toggleCard(e.target.dataset.step); }
  });
  function markCurrentCards() {
    els.tgrid.querySelectorAll('[data-step]').forEach(b => b.setAttribute('aria-current', String(b.dataset.step === S.step)));
    els.list.querySelectorAll('.row').forEach(r => r.setAttribute('aria-current', String(r.dataset.id === S.step)));
  }
  function closePanel(opts) {
    if (S.view === 'transcript') S.step = null;
    els.panel.hidden = true; el('pdivider').hidden = true; els.panel.innerHTML = '';
    markCurrentCards();
    renderCrumbs();
    if (opts?.record !== undefined) writeHash(opts.record ? 'push' : 'replace');
  }

  // ---------- search view: the filtered list of steps
  function select() {
    const txt = S.text.trim().toLowerCase();
    const rmin = S.rmin === '' ? -Infinity : Number(S.rmin), rmax = S.rmax === '' ? Infinity : Number(S.rmax);
    matches = STEPS.filter(s => (!S.agent || s.agent === S.agent)
      && (!S.tool || s.tools.some(t => t.function === S.tool))
      && (!S.verdict || (S.verdict === 'flagged' ? flagged(s) : S.verdict === 'notflagged' ? !flagged(s) : s.peak == null))
      && (!S.covert || (S.covert === 'covert' ? s.covert.length > 0 : s.covert.length === 0))
      && s.round >= rmin && s.round <= rmax
      && (!S.noworld || s.agent !== WORLD)
      && (!txt || SEARCH.get(s.id).includes(txt))
      && (!S.mine || noteWritten(noteOf(s.id)))
      && (!S.unread || !noteRead(noteOf(s.id))));
    sampleInfo = S.sample ? sampleOf(matches) : null;
    if (sampleInfo) matches = sampleInfo.picked;
    matches.sort(comparatorFor(S.sort));
    renderChips(); renderList(true); renderSampleRow(); renderCrumbs(); refreshNav(); syncControls();
  }
  function comparatorFor(sort) {
    if (sort === 'peak') return (a, b) => (b.peak ?? -1) - (a.peak ?? -1) || byRound(a, b);
    if (sort === 'lane') return (a, b) => laneIndex(a.agent) - laneIndex(b.agent) || a.round - b.round;
    return byRound;
  }

  const rng = seed => { let a = (Number(seed) || 1) >>> 0; return () => { a = (a + 0x6d2b79f5) >>> 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; };
  const drawFrom = (pool, r, want) => { const a = [...pool]; for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a.slice(0, Math.max(0, want)); };
  let sampleInfo = null;
  // Balanced on purpose: flagged steps are a small share of a run, so an unstratified
  // draw would show a page of routine steps and hide the ones worth checking.
  function sampleOf(all) {
    const r = rng(S.seed), flagPool = all.filter(flagged), cleanPool = all.filter(s => !flagged(s));
    const f = drawFrom(flagPool, r, Number(S.sflag) || 0), c = drawFrom(cleanPool, r, Number(S.sclean) || 0);
    return { picked: [...f, ...c], f: f.length, c: c.length, flagPool: flagPool.length, cleanPool: cleanPool.length };
  }
  function renderSampleRow() {
    el('samplerow').hidden = !S.sample;
    el('btn-sample').setAttribute('aria-pressed', String(S.sample));
    el('btn-sample').classList.toggle('on', S.sample);
    if (!S.sample || !sampleInfo) return;
    const s = sampleInfo;
    el('s-note').innerHTML = `<b style="color:var(--flag)">${s.f} flagged</b> of ${s.flagPool} and <b style="color:var(--clean)">${s.c} not flagged</b> of ${s.cleanPool}, drawn with seed <code>${esc(String(S.seed))}</code>. The seed is in the address bar, so this draw can be shared or reopened.`;
  }

  function renderChips() {
    const active = [];
    if (S.agent) active.push(['lane', S.agent, 'agent']);
    if (S.tool) active.push(['tool', S.tool, 'tool']);
    if (S.verdict) active.push(['monitors', { flagged: 'flagged', notflagged: 'not flagged', none: 'not scored' }[S.verdict], 'verdict']);
    if (S.covert) active.push(['', S.covert === 'covert' ? 'covert' : 'not covert', 'covert']);
    if (S.rmin !== '') active.push(['from', roundLabel(Number(S.rmin)), 'rmin']);
    if (S.rmax !== '') active.push(['to', roundLabel(Number(S.rmax)), 'rmax']);
    if (S.text) active.push(['text', `"${S.text}"`, 'text']);
    if (S.noworld) active.push(['', 'no World', 'noworld']);
    if (S.mine) active.push(['', 'has my note', 'mine']);
    if (S.unread) active.push(['', 'unread only', 'unread']);
    if (S.sample) active.push(['sample', `${S.sflag} flagged + ${S.sclean} not, seed ${S.seed}`, 'sample']);
    const f = matches.filter(flagged).length, c = matches.filter(s => s.covert.length).length, missed = matches.filter(s => s.covert.length && !flagged(s)).length;
    const stat = matches.length
      ? `<span><b class="num" style="color:var(--ink)">${matches.length}</b> step${matches.length === 1 ? '' : 's'} &middot; <b class="num" style="color:var(--flag)">${f}</b> flagged (score above ${THR}) &middot; <b class="num" style="color:var(--covert)">${c}</b> covert, ${missed} of them not flagged</span>`
      : '<span>Nothing matches these filters.</span>';
    els.chips.innerHTML = active.map(([k, v, field]) => `<button class="chip" data-clear="${field}" title="remove">${k ? `<i>${esc(k)}</i>` : ''}${esc(v)} &times;</button>`).join('') + stat;
    els.chips.querySelectorAll('[data-clear]').forEach(b => b.addEventListener('click', () => {
      const field = b.dataset.clear;
      S[field] = typeof DEFAULT_STATE[field] === 'boolean' ? false : '';
      select(); writeHash('replace');
    }));
  }

  function refreshRowMark(id) {
    els.tgrid.querySelectorAll(`.tc[data-step="${CSS.escape(id)}"]:not(.v)`).forEach(c => c.classList.toggle('mine', noteWritten(noteOf(id))));
    const row = els.list.querySelector(`.row[data-id="${CSS.escape(id)}"]`);
    if (!row) return;
    const own = noteOf(id);
    row.classList.toggle('mine', noteWritten(own));
    row.classList.toggle('read', noteRead(own));
  }
  // Marking read does not re-run `select()`, so "unread only" does not pull the step
  // out of the list mid-read. The filter applies the next time the selection rebuilds.
  function setRead(id, read) {
    const n = noteFor(id);
    if (read) n.read = true; else delete n.read;
    touch(id);
    refreshReadButton();
  }
  function refreshReadButton() {
    const btn = readerHost()?.querySelector('[data-act="read"]'); if (!btn) return;
    const own = S.step ? noteOf(S.step) : null;
    const written = noteWritten(own), read = noteRead(own);
    btn.setAttribute('aria-pressed', String(read));
    btn.classList.toggle('on', read);
    btn.innerHTML = icon('check', 15) + (read ? 'Read' : 'Mark read');
    btn.disabled = written;
    btn.title = written ? 'Your note on this step already counts as having read it.' : read ? 'Marked read. Press again to undo. Shortcut: r' : 'Mark this step read. Shortcut: r';
  }

  function renderList(reset) {
    if (reset) { els.list.innerHTML = ''; shown = 0; }
    const slice = matches.slice(shown, shown + PAGE);
    els.list.insertAdjacentHTML('beforeend', slice.map(s => {
      const own = noteOf(s.id), t = s.tools[0];
      const cls = ['row', noteWritten(own) ? 'mine' : '', noteRead(own) ? 'read' : '', s.covert.length ? 'covert' : ''].filter(Boolean).join(' ');
      const what = t ? `${t.function}: ${oneLine(t.action)}` : stepSummary(s);
      return `<button class="${cls}" data-id="${esc(s.id)}" aria-current="${S.step === s.id}"><span class="k">${roundKey(s.round)}</span>${pill(s.peak)}<span class="lab">${kindIcon(headKindOf(s), 12)}<b>${esc(s.agent)}</b> · ${esc(what)}</span><span class="ch"><span class="dot" title="covert"></span><span class="pen">${icon('pencil', 12)}</span><span class="tick">${icon('check', 12)}</span></span></button>`;
    }).join(''));
    shown += slice.length;
    els.count.innerHTML = `showing <b class="num">${Math.min(shown, matches.length)}</b> of <b class="num">${matches.length}</b>`;
    els.more.hidden = shown >= matches.length;
    els.more.textContent = `Show ${Math.min(PAGE, matches.length - shown)} more`;
  }
  els.more.addEventListener('click', () => renderList(false));
  el('copy-selection').addEventListener('click', ev => copyText(JSON.stringify({
    run: RUN.name,
    selected_at: new Date().toISOString(),
    filters: Object.fromEntries(Object.keys(FILTER_DEFAULTS).map(k => [k, S[k]])),
    n: matches.length,
    steps: matches.map(s => ({ id: s.id, lane: s.agent, round: s.round, tools: s.tools.map(t => t.function), peak_suspicion: s.peak, covert: s.covert, verdicts: s.verdicts.map(v => ({ monitor: v.monitor, score: v.score, reason: v.reason })) })),
  }, null, 1), ev.currentTarget));
  els.list.addEventListener('click', e => { const b = e.target instanceof Element ? e.target.closest('.row[data-id]') : null; if (b) openStep(byId.get(b.dataset.id), { record: true }); });

