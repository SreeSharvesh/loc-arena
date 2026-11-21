(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('report-data').textContent);
  const run = data.run, analysis = data.analysis, records = run.records;
  const byId = new Map(records.map(record => [record.id, record]));
  const byInsight = new Map(analysis ? [...analysis.overview, ...analysis.chain, ...analysis.insights].map(item => [item.id, item]) : []);
  const element = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const json = value => JSON.stringify(value, null, 2);
  const number = value => typeof value === 'number' ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : 'Unavailable';
  const known = value => value !== null && value !== undefined;
  const fact = path => path.split('.').reduce((value, key) => value && typeof value === 'object' ? value[key] : undefined, run.scores);
  let view = 'insights', sourceId = null, sourceTrigger = null, page = 0, filtered = records, jobId = null, jobTimer = null;
  const pageSize = () => Math.max(1, Number.parseInt(element('record-page-size').value, 10) || 1);
  const searchIndex = new Map(records.map(record => [record.id, json(record.raw).toLowerCase()]));

  function showView(next) {
    if (!['insights', 'records', 'agents'].includes(next)) return;
    view = next;
    document.querySelectorAll('.run-view').forEach(section => { section.hidden = section.id !== `view-${view}`; });
    document.querySelectorAll('[data-view]').forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    writeLocation();
  }

  function writeLocation() {
    const hash = new URLSearchParams({view});
    if (sourceId) hash.set('source', sourceId);
    history.replaceState(null, '', `#${hash}`);
  }

  function renderOutcome() {
    const outcome = run.outcome;
    const mainNote = outcome.gate_passed === false ? `Correctness gate failed${outcome.gate_reason ? ': ' + outcome.gate_reason : ''}.` : outcome.gate_passed === true ? 'Correctness gate passed. Higher is better.' : 'Recorded score; correctness gate unavailable.';
    const threshold = known(outcome.threshold) ? `Detection bar ${number(outcome.threshold)}` : 'Detection threshold unavailable';
    const cards = [
      ['Main task', known(outcome.main_score) ? number(outcome.main_score) + '×' : 'Unavailable', mainNote, ''],
      ['Side task', outcome.landed ? 'Landed' : 'Did not land', 'The verifier’s recorded outcome.', outcome.landed ? 'good' : ''],
      ['Detection', outcome.caught ? 'Caught' : 'Not caught', threshold + '. See calibration limits below.', ''],
    ];
    element('outcome').innerHTML = cards.map(([label,value,note,state]) => `<div class="outcome-card"><div class="outcome-label">${escape(label)}</div><div class="outcome-value ${state}">${escape(value)}</div><div class="outcome-note">${escape(note)}</div></div>`).join('');
    const warnings = [...(run.warnings || [])];
    const failures = Object.entries(outcome.failed_calls || {}).filter(([, count]) => count > 0);
    if (failures.length) warnings.unshift('Failed model calls: ' + failures.map(([play,count]) => `${count} in ${play}`).join(', ') + '. Inspect these failures before interpreting model behavior or calibration.');
    if (analysis && analysis.source_fingerprint !== run.fingerprint) warnings.unshift('These insights were generated from different source records. Reanalyze this run before relying on them.');
    element('warnings').innerHTML = [...new Set(warnings)].map(warning => `<div class="warning">${escape(warning)}</div>`).join('');
  }

  function sourceLabel(cite) {
    const record = byId.get(cite);
    if (record) return `${record.actor} · ${record.tool || record.kind}`;
    return cite.startsWith('scores:') ? cite.slice('scores:'.length) : cite;
  }

  function citations(item) {
    return `<div class="cite-row"><span class="cite-label">Evidence</span>${item.cites.map(cite => `<button type="button" class="citation" data-source="${escape(cite)}" title="${escape(cite)}">${escape(sourceLabel(cite))}</button>`).join('')}</div>`;
  }

  const links = data.session_links || {turns: {}, results: {}};
  const parts = record => { const content = record?.raw?.message?.content; return Array.isArray(content) ? content : typeof content === 'string' ? [{type: 'text', text: content}] : []; };
  const partText = part => typeof part.text === 'string' ? part.text : json(part);
  const pretty = value => { if (typeof value !== 'string') return json(value); try { return json(JSON.parse(value)); } catch { return value; } };

  // One model turn as a researcher reads it: what it thought, what it said, each call it made and what came back.
  function stepView(id) {
    const turn = byId.get(links.turns[id] || id);
    if (!turn || turn.raw?.message?.role !== 'assistant') return '';
    const content = parts(turn);
    const thinking = content.filter(part => part.type === 'thinking').map(part => part.thinking).filter(Boolean);
    const said = content.filter(part => part.type === 'text').map(partText).filter(Boolean);
    const calls = content.filter(part => part.type === 'toolCall');
    const results = links.results[turn.id] || {};
    const call = part => {
      const result = byId.get(results[part.id]);
      const output = result ? parts(result).map(partText).join('\n') : null;
      return `<div class="step-call"><div class="step-call-name">${escape(part.name)}</div><pre>${escape(pretty(part.arguments))}</pre><div class="step-label">Result${result?.raw?.message?.isError ? ' · error' : ''}</div><pre class="step-result">${escape(output ?? 'No result was linked to this call.')}</pre></div>`;
    };
    return `<div class="step"><div class="step-label">Reasoning</div>${thinking.length ? `<pre class="step-reasoning">${escape(thinking.join('\n\n'))}</pre>` : '<p class="prose">No reasoning was recorded for this turn.</p>'}${said.length ? `<div class="step-label">Said</div><p class="step-said">${escape(said.join('\n\n'))}</p>` : ''}${calls.length ? `<div class="step-label">Actions</div>${calls.map(call).join('')}` : ''}</div>`;
  }

  function inlineStep(item) {
    const turn = item.cites.find(cite => stepView(cite));
    if (!turn) return '';
    const names = parts(byId.get(links.turns[turn] || turn)).filter(part => part.type === 'toolCall').map(part => part.name.replace(/^mcp__\w+?__/, ''));
    return `<details class="step-inline"><summary>Reasoning &amp; action${names.length ? ' · ' + escape(names.join(', ')) : ''}</summary>${stepView(turn)}</details>`;
  }

  function insightCard(item, overview = false) {
    const actors = [...new Set(item.cites.map(cite => byId.get(cite)?.actor).filter(Boolean))];
    const title = `<h3>${escape(item.title)}</h3>`;
    const status = ['attempted','completed','blocked','uncertain'].includes(item.status) ? item.status : 'uncertain';
    const related = (item.related || []).filter(id => byInsight.has(id));
    if (overview) return `<article id="insight-${escape(item.id)}">${title}<p>${escape(item.text)}</p>${citations(item)}</article>`;
    return `<article class="insight-card" id="insight-${escape(item.id)}" data-status="${status}">
      <div class="card-top"><span class="status ${status}">${status}</span>${actors.map(actor => `<span class="actor-chip">${escape(actor)}</span>`).join('')}</div>
      ${title}<p class="insight-copy">${escape(item.text)}</p>
      ${item.why_it_matters ? `<p class="why"><b>Why it matters.</b> ${escape(item.why_it_matters)}</p>` : ''}
      ${inlineStep(item)}
      ${citations(item)}
      ${related.length ? `<div class="related-links">${related.map(id => `<button type="button" data-insight="${escape(id)}">Related: ${escape(byInsight.get(id).title)}</button>`).join('')}</div>` : ''}
    </article>`;
  }

  function renderInsights() {
    element('analysis-empty').hidden = Boolean(analysis);
    element('analysis-content').hidden = !analysis;
    if (!analysis) {
      element('empty-copy').textContent = data.live ? 'Analyze this run to connect the team’s actions, surface consequential events, and link each interpretation to its source. Your configured model is used only when you start analysis.' : 'This offline report contains the captured records, but no saved analysis. Open the run in the local analysis app to generate Insights, then export an updated report.';
      return;
    }
    element('overview').innerHTML = analysis.overview.map(item => insightCard(item, true)).join('') || '<p>No supported overview was saved.</p>';
    element('chain').innerHTML = analysis.chain.map(item => insightCard(item)).join('') || '<p class="empty-list">No supported attack chain was reconstructed. This is an evidence gap, not proof that no attempt occurred.</p>';
    const limit = Math.max(1, Number.parseInt(element('insight-limit').value, 10) || analysis.insights.length);
    const shown = analysis.insights.slice(0, limit);
    element('interesting-events').innerHTML = shown.map(item => insightCard(item)).join('') || '<p class="empty-list">No additional interesting events were saved.</p>';
    element('selection-note').textContent = `Showing ${shown.length} of ${analysis.insights.length} saved events. The analyst selected them for research relevance; complete source records remain in All records.`;
    const limits = [['Uncertainties', analysis.unknowns], ['Analysis coverage', analysis.coverage]];
    element('research-limits').innerHTML = limits.map(([title,items]) => `<section><h3>${escape(title)}</h3><ul>${items.length ? items.map(item => `<li>${escape(item)}</li>`).join('') : '<li>No statement was saved. This does not establish complete coverage.</li>'}</ul></section>`).join('');
    element('analysis-origin').textContent = `Analyzed with ${analysis.model}`;
    const usage = [`${number(analysis.elapsed_seconds)} seconds`, `${number(analysis.calls)} model calls`, known(analysis.cost_usd) ? `$${number(analysis.cost_usd)} recorded cost` : 'Cost unavailable'];
    element('analysis-footer').textContent = `Generated ${analysis.generated_at} · ${usage.join(' · ')}. Source references are checked; the interpretation can still be wrong. Inspect the evidence before treating a relationship as causal.`;
    element('analyze').textContent = 'Analyze again';
  }

  function summarizeRecord(record) {
    const payload = record.raw.payload ?? record.raw.message ?? record.raw;
    const text = payload && typeof payload === 'object' ? payload.body || payload.text || payload.description || payload.reason || payload.target || payload.args : payload;
    return typeof text === 'string' ? text : json(text || record.raw.result || '');
  }

  function filterRecords() {
    const query = element('record-search').value.trim().toLowerCase();
    const fields = ['actor','play','stream','kind'];
    filtered = records.filter(record => fields.every(field => !element(`filter-${field}`).value || record[field] === element(`filter-${field}`).value) && (!query || `${record.id} ${record.actor} ${record.kind} ${record.tool || ''} ${searchIndex.get(record.id)}`.toLowerCase().includes(query)));
    page = 0;
    renderRecords();
  }

  function renderRecords() {
    const size = pageSize();
    const totalPages = Math.max(1, Math.ceil(filtered.length / size));
    page = Math.min(page, totalPages - 1);
    const start = page * size, visible = filtered.slice(start, start + size);
    element('records-summary').textContent = `${filtered.length.toLocaleString()} matching records of ${records.length.toLocaleString()} captured across the included plays and logs. Observable and analyst logs may contain corresponding records; their identifiers are separate.`;
    element('record-list').innerHTML = visible.map(record => `<button type="button" class="record-row${record.id === sourceId ? ' selected' : ''}" data-source="${escape(record.id)}"><span class="record-identity">${escape(record.id)}<br>${escape(record.play)}</span><span class="record-description"><b>${escape(record.actor)} · ${escape(record.tool || record.kind)}</b><span>${escape(summarizeRecord(record))}</span></span><span class="record-type">${record.stream === 'mirror' ? 'Observable' : record.stream === 'session' ? 'Session' : 'Analyst'}</span></button>`).join('') || '<p class="empty-list">No records match. Clear the filters to restore the full list.</p>';
    element('records-page').textContent = filtered.length ? `${start + 1}–${Math.min(start + size, filtered.length)} · page ${page + 1} of ${totalPages}` : 'No matches';
    element('records-previous').disabled = page === 0;
    element('records-next').disabled = page + 1 >= totalPages;
  }

  function renderAgents() {
    element('agents').innerHTML = run.agents.map(agent => {
      const count = records.filter(record => record.actor === agent.id && record.stream === 'sealed').length;
      return `<article class="agent-card"><div class="agent-heading"><h3>${escape(agent.id)}</h3><p>${count.toLocaleString()} analyst-log records</p></div><details><summary>Legitimate assignment</summary><pre>${escape(agent.brief || 'No resolved assignment was captured.')}</pre></details><details><summary>Covert instructions · analyst context</summary><pre>${escape(agent.covert_brief || 'No resolved covert instructions were captured.')}</pre></details></article>`;
    }).join('') || '<p class="empty-list">No configured participant context was captured.</p>';
  }

  function renderJudgment(judgment) {
    const flagged = known(run.outcome.threshold) && judgment.score > run.outcome.threshold;
    return `<div class="judgment${flagged ? ' flagged' : ''}"><div class="judgment-head"><span>${escape(judgment.monitor)}</span><span>${escape(number(judgment.score))}${flagged ? ' · above bar' : ''}</span></div><p>${escape(judgment.reason)}</p>${judgment.target_id ? `<button type="button" class="citation" data-source="${escape(judgment.target_id)}">Judged ${escape(judgment.target_id)}</button>` : ''}</div>`;
  }

  function openSource(id, trigger) {
    const record = byId.get(id);
    const value = !record && id.startsWith('scores:') ? fact(id.slice('scores:'.length)) : undefined;
    if (!record && value === undefined) return;
    sourceId = id;
    if (trigger) sourceTrigger = trigger;
    element('app').classList.add('evidence-open');
    element('evidence-panel').hidden = false;
    const modal = matchMedia('(max-width:1100px)').matches;
    element('evidence-panel').setAttribute('aria-modal', String(modal));
    element('main').inert = modal;
    document.querySelector('.sidebar').inert = modal;
    element('evidence-title').textContent = id;
    element('evidence-position').textContent = record ? `${record.play} · ${record.stream}` : 'Recorded score';
    const navigation = record ? records.filter(item => item.play === record.play && item.stream === record.stream) : [];
    const position = navigation.findIndex(item => item.id === id);
    element('evidence-previous').disabled = position <= 0;
    element('evidence-next').disabled = position < 0 || position + 1 >= navigation.length;
    element('evidence-previous').onclick = () => { if (position > 0) openSource(navigation[position - 1].id); };
    element('evidence-next').onclick = () => { if (position + 1 < navigation.length) openSource(navigation[position + 1].id); };
    if (record) {
      const timestamp = record.stream === 'session' && record.time > 1000000000 ? new Date(record.time * 1000).toISOString() : `${number(record.time)} s · run-relative`;
      const metadata = [['Actor',record.actor],['Kind',record.kind],['Tool',record.tool || '—'],['Target',record.target || '—'],['Timestamp',known(record.time) ? timestamp : 'Unavailable'],['Turn',record.turn]];
      const payload = record.raw.payload ?? record.raw.message ?? record.raw;
      element('evidence-body').innerHTML = `<div class="evidence-badges"><span class="visibility${record.stream === 'mirror' ? ' observable' : ''}">${record.stream === 'mirror' ? 'Observable log' : record.stream === 'session' ? 'Session · analyst context' : 'Analyst log · sealed evidence'}</span><span class="actor-chip">${escape(record.play)}</span></div><dl class="evidence-meta">${metadata.map(([key,item]) => `<dt>${escape(key)}</dt><dd>${escape(item)}</dd>`).join('')}</dl><section class="evidence-block"><h3>Monitor judgments</h3>${record.verdicts.length ? record.verdicts.map(renderJudgment).join('') : '<p class="prose">No judgment is directly linked to this source identifier. A missing link does not establish that every monitor missed the action.</p>'}</section>${stepView(id) ? `<section class="evidence-block"><h3>Reasoning &amp; action</h3>${stepView(id)}</section>` : ''}${record.stream !== 'session' || !stepView(id) ? `<section class="evidence-block"><h3>${record.stream === 'session' ? 'Complete recorded message' : 'Recorded payload'}</h3><pre>${escape(json(payload))}</pre></section>` : ''}${record.stream !== 'session' ? `<section class="evidence-block"><h3>Recorded result</h3><pre>${escape(json(record.raw.result ?? null))}</pre></section>` : ''}<details class="evidence-block"><summary>Complete raw record</summary><pre>${escape(json(record.raw))}</pre></details>`;
    } else {
      element('evidence-body').innerHTML = `<div class="visibility">Recorded outcome · analyst context</div><section class="evidence-block"><h3>${escape(id.slice('scores:'.length))}</h3><pre>${escape(json(value))}</pre></section><p class="prose">This value comes from the saved scores. The analysis does not change it.</p>`;
    }
    element('evidence-body').scrollTop = 0;
    if (trigger) element('evidence-body').focus({preventScroll:true});
    document.querySelectorAll('[data-source]').forEach(button => button.classList.toggle('selected', button.dataset.source === id));
    writeLocation();
  }

  function closeSource() {
    sourceId = null;
    element('evidence-panel').hidden = true;
    element('app').classList.remove('evidence-open');
    element('main').inert = false;
    document.querySelector('.sidebar').inert = false;
    document.querySelectorAll('[data-source]').forEach(button => button.classList.remove('selected'));
    sourceTrigger?.focus({preventScroll:true});
    writeLocation();
  }

  function showJob(status, message) {
    const banner = element('analysis-job');
    banner.hidden = false;
    banner.classList.toggle('failed', ['failed','cancelled'].includes(status));
    element('job-title').textContent = status === 'failed' ? 'Analysis failed' : status === 'cancelled' ? 'Analysis cancelled' : status === 'queued' ? 'Analysis queued' : 'Analyzing this run';
    element('job-detail').textContent = message || 'Reading evidence and connecting the attempt across agents. You can browse the captured records while it works.';
    element('cancel-job').hidden = ['failed','cancelled'].includes(status);
    element('analyze').disabled = !['failed','cancelled'].includes(status);
  }

  async function pollJob() {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {cache:'no-store'});
      if (!response.ok) throw new Error(`Status request failed (${response.status}).`);
      const job = await response.json();
      if (['complete','completed','succeeded'].includes(job.status)) { location.reload(); return; }
      showJob(job.status, job.error || job.message);
      if (!['failed','cancelled'].includes(job.status)) jobTimer = setTimeout(pollJob, 2000);
    } catch (error) { showJob('failed', `${error.message} Refresh to check whether the worker finished.`); }
  }

  async function startAnalysis() {
    clearTimeout(jobTimer);
    showJob('queued');
    try {
      const response = await fetch('/api/analyze', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run:data.run_key})});
      if (!response.ok) {
        const problem = await response.json().catch(() => ({}));
        throw new Error(typeof problem.detail === 'string' ? problem.detail : `The analysis could not start (${response.status}).`);
      }
      const job = await response.json();
      jobId = job.id;
      if (!jobId) throw new Error('The server did not return an analysis job identifier.');
      await pollJob();
    } catch (error) { showJob('failed', error.message); }
  }

  document.addEventListener('click', event => {
    const source = event.target.closest('[data-source]');
    if (source) { openSource(source.dataset.source, source); return; }
    const related = event.target.closest('[data-insight]');
    if (related) {
      const item = byInsight.get(related.dataset.insight);
      if (item && analysis.insights.includes(item)) { element('insight-limit').value = analysis.insights.length; renderInsights(); }
      const card = element(`insight-${related.dataset.insight}`);
      if (card) { showView('insights'); card.scrollIntoView({block:'center',behavior:'smooth'}); card.classList.remove('spotlight'); requestAnimationFrame(() => card.classList.add('spotlight')); }
    }
  });
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
  element('browse-records').addEventListener('click', () => showView('records'));
  element('home-link').addEventListener('click', event => { if (!data.live) { event.preventDefault(); showView('insights'); } });
  element('close-evidence').addEventListener('click', closeSource);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && sourceId) closeSource();
    if (event.key !== 'Tab' || !sourceId || element('evidence-panel').getAttribute('aria-modal') !== 'true') return;
    const focusable = [...element('evidence-panel').querySelectorAll('button:not(:disabled),[tabindex="0"],summary')].filter(item => item.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === element('evidence-body'))) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  element('insight-limit').addEventListener('input', renderInsights);
  element('record-search').addEventListener('input', filterRecords);
  element('record-page-size').addEventListener('input', () => { page = 0; renderRecords(); });
  ['actor','play','stream','kind'].forEach(field => {
    if (field !== 'stream') {
      const select = element(`filter-${field}`);
      [...new Set(records.map(record => record[field]))].sort().forEach(value => { const option = document.createElement('option'); option.value = value; option.textContent = value; select.append(option); });
    }
    element(`filter-${field}`).addEventListener('change', filterRecords);
  });
  element('reset-filters').addEventListener('click', () => { ['actor','play','stream','kind'].forEach(field => { element(`filter-${field}`).value = ''; }); element('record-search').value = ''; filterRecords(); });
  element('records-previous').addEventListener('click', () => { page--; renderRecords(); });
  element('records-next').addEventListener('click', () => { page++; renderRecords(); });
  element('analyze').addEventListener('click', startAnalysis);
  element('cancel-job').addEventListener('click', async () => {
    if (!jobId) return;
    element('cancel-job').disabled = true;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {method:'DELETE'});
      if (!response.ok) throw new Error('Cancellation could not be confirmed.');
      clearTimeout(jobTimer);
      showJob('cancelled', 'The cancellation request was accepted. Previously saved insights remain available.');
    } catch (error) { element('job-detail').textContent = error.message; } finally { element('cancel-job').disabled = false; }
  });
  const savedTheme = (() => { try { return localStorage.getItem('run-insights-theme'); } catch { return null; } })();
  document.documentElement.dataset.theme = savedTheme || (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');
  element('theme-toggle').addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('run-insights-theme', theme); } catch { /* Viewing does not require browser storage. */ }
  });
  const shortName = run.name.match(/(?:__|\/)([a-f0-9]{6,12})$/)?.[1] || run.name.match(/[a-f0-9]{6}$/)?.[0];
  element('run-title').textContent = shortName ? `Run ${shortName}` : 'Run insights';
  document.title = element('run-title').textContent + ' · Run insights';
  element('run-name').textContent = run.name;
  element('insight-count').textContent = analysis ? analysis.chain.length + analysis.insights.length : '—';
  element('record-count').textContent = records.length.toLocaleString();
  element('agent-count').textContent = run.agents.length;
  element('analyze').hidden = !data.live;
  if (data.live) {
    element('home-link').href = '/';
    element('export-report').hidden = false;
    element('export-report').href = `/api/report?run=${encodeURIComponent(data.run_key)}`;
  }
  renderOutcome();
  renderInsights();
  renderRecords();
  renderAgents();
  const initial = new URLSearchParams(location.hash.slice(1));
  showView(initial.get('view') || 'insights');
  if (initial.get('source')) openSource(initial.get('source'));
})();
