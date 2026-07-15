/** AgentOS Web UI — Skills Management view. */

const SkillsView = (() => {
  let _el = null;
  let _rpc = null;
  let _unsubs = [];
  let _intervals = [];
  let _allSkills = [];
  let _filterText = '';
  let _statusFilter = 'all';
  let _activeTab = 'installed';

  // Community/Bankr browse state.
  let _registryCache = { bankr: null, community: null }; // source group → results[]
  let _registryLoading = { bankr: false, community: false };
  let _catFilter = { bankr: 'all', community: 'all' };
  let _registryQuery = { bankr: '', community: '' };

  const _LAYER_ORDER = ['workspace', 'bundled', 'managed', 'personal', 'project', 'extra'];
  const _LAYER_LABEL = {
    workspace: 'Workspace',
    bundled: 'Bundled',
    managed: 'Managed',
    personal: 'Personal',
    project: 'Project',
    extra: 'Extra',
  };
  const _LAYER_HELP = {
    workspace: 'Workspace skills are local to the active workspace.',
    bundled: 'Bundled skills ship with AgentOS.',
    managed: 'Managed skills are locally installed into AgentOS state.',
    personal: 'Personal skills are local user installs, not bundled.',
    project: 'Project skills are local to the current project.',
    extra: 'Extra skills come from configured local directories.',
  };

  const _CAT_LABEL = {
    all: 'All', trading: 'Trading', defi: 'DeFi', wallet: 'Wallets',
    markets: 'Markets', social: 'Social', data: 'Data', nft: 'NFT',
    dev: 'Dev tools', infra: 'Infra', other: 'Other',
  };

  function _ensureCss() {
    if (document.querySelector('link[data-view-css="skills"]')) return;
    const data = document.getElementById('agentos-data');
    const base = data?.dataset.basePath || '';
    const cssVersion = data?.dataset.version || '';
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = `${base}/static/css/views/skills.css${cssVersion ? '?v=' + encodeURIComponent(cssVersion) : ''}`;
    link.dataset.viewCss = 'skills';
    document.head.appendChild(link);
  }

  function render(el) {
    _el = el;
    _rpc = App.getRpc();
    _registryCache = { bankr: null, community: null };
    _registryLoading = { bankr: false, community: false };
    _catFilter = { bankr: 'all', community: 'all' };
    _registryQuery = { bankr: '', community: '' };
    _activeTab = 'installed';
    _ensureCss();

    _el.innerHTML = `
      <div class="sk-stage">
        <header class="sk-hero">
          <div class="sk-hero__top">
            <div class="sk-hero__intro">
              <span class="sk-hero__eyebrow">${icons.skills()}<span>Control · Skills</span></span>
              <h2 class="sk-hero__title">Skills</h2>
              <p class="sk-hero__subtitle">Composable agent capabilities — bundled packs, the Bankr partner catalog, and the wider community.</p>
            </div>
            <div class="sk-hero__actions">
              <div class="sk-search-wrap" id="sk-search-wrap">
                <span class="sk-search-icon">${icons.search()}</span>
                <input class="sk-search-input" type="search" id="skills-filter" placeholder="Filter installed…" autocomplete="off" />
              </div>
              <button class="sk-iconbtn" id="skills-refresh" title="Refresh" aria-label="Refresh">
                ${icons.refresh()}
              </button>
            </div>
          </div>
          <div class="sk-metrics" id="sk-stats"></div>
        </header>

        <div class="sk-tabs" role="group" aria-label="Skill source">
          <button class="sk-tab is-active" data-tab="installed" aria-pressed="true">${icons.skills()}<span>Installed</span></button>
          <button class="sk-tab sk-tab--bankr" data-tab="bankr" aria-pressed="false">${_bankrGlyph()}<span>Bankr</span></button>
          <button class="sk-tab" data-tab="community" aria-pressed="false">${icons.download()}<span>Community</span></button>
        </div>

        <div id="skills-tab-installed" class="sk-panel">
          <div id="skills-installed-wrap"></div>
        </div>

        <div id="skills-tab-bankr" class="sk-panel" hidden>
          <div class="sk-partner">
            <div class="sk-partner__mark">${_bankrGlyph(48)}</div>
            <div class="sk-partner__text">
              <div class="sk-partner__name">Bankr partner catalog</div>
              <p class="sk-partner__desc">Plug-and-play on-chain skills — trading, wallets, DeFi, markets, and more — installed straight from <span class="sk-mono">BankrBot/skills</span>.</p>
            </div>
            <a class="sk-partner__link" href="https://bankr.bot" target="_blank" rel="noopener">bankr.bot ↗</a>
          </div>
          <div class="sk-browse" data-group="bankr">
            <div class="sk-browse__bar">
              <div class="sk-search-wrap sk-search-wrap--lg">
                <span class="sk-search-icon">${icons.search()}</span>
                <input class="sk-search-input sk-search-input--lg" type="search" data-registry-search="bankr" placeholder="Search Bankr skills…" autocomplete="off" />
              </div>
            </div>
            <div class="sk-chips" data-chips="bankr"></div>
            <div class="sk-browse__results" data-results="bankr"></div>
          </div>
        </div>

        <div id="skills-tab-community" class="sk-panel" hidden>
          <div class="sk-browse" data-group="community">
            <div class="sk-browse__bar">
              <div class="sk-search-wrap sk-search-wrap--lg">
                <span class="sk-search-icon">${icons.search()}</span>
                <input class="sk-search-input sk-search-input--lg" type="search" data-registry-search="community" placeholder="Search community skills…" autocomplete="off" />
              </div>
            </div>
            <div class="sk-github-install">
              <div class="sk-search-wrap sk-search-wrap--lg">
                <span class="sk-search-icon">${icons.download()}</span>
                <input class="sk-search-input sk-search-input--lg" type="url" id="skills-github-url" placeholder="https://github.com/owner/repo/tree/main/path/to/skill" autocomplete="off" />
              </div>
              <button class="btn btn--primary" id="skills-github-install">Install GitHub URL</button>
            </div>
            <div class="sk-chips" data-chips="community"></div>
            <div class="sk-browse__results" data-results="community"></div>
          </div>
        </div>

        <dialog id="skill-detail-dialog" class="sk-dialog">
          <div id="skill-detail-body"></div>
        </dialog>
      </div>`;

    const _dlg = _el.querySelector('#skill-detail-dialog');
    if (_dlg) {
      _dlg.addEventListener('click', (e) => { if (e.target === _dlg) _dlg.close(); });
    }

    const _filterInput = _el.querySelector('#skills-filter');
    const _searchWrap = _el.querySelector('#sk-search-wrap');
    _el.querySelectorAll('.sk-tab').forEach(btn => {
      btn.addEventListener('click', () => _selectTab(btn.dataset.tab, _searchWrap));
    });

    _el.querySelector('#skills-refresh').addEventListener('click', () => {
      if (_activeTab === 'installed') { _loadData(); return; }
      _registryCache[_activeTab] = null;
      _browse(_activeTab, _registryQuery[_activeTab]);
    });

    _filterInput.addEventListener('input', () => {
      _filterText = _filterInput.value.toLowerCase();
      _renderCards();
    });

    // Registry search inputs (Bankr + Community).
    _el.querySelectorAll('[data-registry-search]').forEach(input => {
      const group = input.dataset.registrySearch;
      input.addEventListener('input', _debounce(() => {
        _registryQuery[group] = input.value;
        _renderRegistry(group);
      }, 160));
    });

    const githubBtn = _el.querySelector('#skills-github-install');
    const githubInput = _el.querySelector('#skills-github-url');
    if (githubBtn && githubInput) {
      githubBtn.addEventListener('click', () => {
        if (githubInput.value.trim()) _installSkill(githubInput.value.trim(), 'github', githubBtn);
      });
      githubInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && githubInput.value.trim()) _installSkill(githubInput.value.trim(), 'github', githubBtn);
      });
    }

    // Delegated clicks.
    _el.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-cat-chip]');
      if (chip) {
        const group = chip.dataset.group;
        _catFilter[group] = chip.dataset.catChip;
        _renderRegistry(group);
        return;
      }
      const installBtn = e.target.closest('[data-install]');
      if (installBtn) {
        _installSkill(installBtn.dataset.install, installBtn.dataset.source || 'clawhub', installBtn);
        return;
      }
      const uninstallBtn = e.target.closest('[data-uninstall]');
      if (uninstallBtn) {
        _uninstallSkill(uninstallBtn.dataset.uninstall, uninstallBtn);
        return;
      }
      const statusPill = e.target.closest('[data-status-filter]');
      if (statusPill) {
        _statusFilter = statusPill.dataset.statusFilter;
        _renderStats();
        _renderCards();
        return;
      }
      const depsBtn = e.target.closest('[data-install-deps-name]');
      if (depsBtn) {
        _installDeps(depsBtn.dataset.installDepsName, depsBtn.dataset.installDepsId, depsBtn);
        return;
      }
      const regCard = e.target.closest('[data-registry-card]');
      if (regCard) {
        const group = regCard.dataset.group;
        const list = _registryCache[group] || [];
        const item = list.find(r => (r.identifier || r.name) === regCard.dataset.registryCard);
        if (item) _openRegistryDialog(item);
        return;
      }
      const card = e.target.closest('[data-skill-card]');
      if (card) {
        const skill = _allSkills.find(s => s.name === card.dataset.skillCard);
        if (skill) _openSkillDialog(skill);
      }
    });

    _loadData();
  }

  function _selectTab(tab, searchWrap) {
    _activeTab = tab;
    _el.querySelectorAll('.sk-tab').forEach(b => {
      const active = b.dataset.tab === tab;
      b.classList.toggle('is-active', active);
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    _el.querySelectorAll('.sk-panel').forEach(p => { p.hidden = true; });
    const panel = _el.querySelector('#skills-tab-' + tab);
    if (panel) panel.hidden = false;
    if (searchWrap) searchWrap.style.visibility = tab === 'installed' ? '' : 'hidden';
    if ((tab === 'bankr' || tab === 'community') && _registryCache[tab] === null && !_registryLoading[tab]) {
      _browse(tab, '');
    }
  }

  function destroy() {
    _unsubs.forEach(fn => fn());
    _unsubs = [];
    _intervals.forEach(id => clearInterval(id));
    _intervals = [];
    _allSkills = [];
    _el = null;
    _rpc = null;
  }

  async function _loadData() {
    if (!_el) return;
    await _rpc.waitForConnection();
    try {
      const data = await _rpc.call('skills.list');
      _allSkills = data.skills || [];
      _renderStats();
      _renderCards();
    } catch (err) {
      const wrap = _el && _el.querySelector('#skills-installed-wrap');
      if (wrap) {
        wrap.innerHTML = `<div class="sk-error">Failed to load skills: ${_esc(err.message)}</div>`;
      }
    }
  }

  function _renderStats() {
    if (!_el) return;
    const wrap = _el.querySelector('#sk-stats');
    if (!wrap) return;

    const total = _allSkills.length;
    const ready = _allSkills.filter(s => s.status === 'ready').length;
    const needs = _allSkills.filter(s => s.status === 'needs_setup').length;
    const notDeclared = _allSkills.filter(s => s.status === 'not_declared').length;

    const pill = (key, label, value, tone = '') => {
      const active = _statusFilter === key;
      return `<button class="sk-metric${tone ? ' sk-metric--' + tone : ''}${active ? ' is-active' : ''}" data-status-filter="${key}" type="button" title="Filter: ${label}">
        <span class="sk-metric__value">${value}</span>
        <span class="sk-metric__label">${label}</span>
      </button>`;
    };

    wrap.innerHTML = `
      ${pill('all', 'All', total, 'accent')}
      <span class="sk-metric__sep"></span>
      ${pill('ready', 'Ready', ready, 'ok')}
      ${pill('needs-setup', 'Needs setup', needs, 'warn')}
      ${pill('not-declared', 'No manifest', notDeclared)}
    `;
  }

  function _renderCards() {
    if (!_el) return;
    const wrap = _el.querySelector('#skills-installed-wrap');
    if (!wrap) return;

    let skills = _allSkills;
    if (_filterText) {
      skills = skills.filter(s =>
        (s.name || '').toLowerCase().includes(_filterText) ||
        (s.description || '').toLowerCase().includes(_filterText) ||
        (s.triggers || []).some(t => t.toLowerCase().includes(_filterText))
      );
    }
    if (_statusFilter === 'ready') {
      skills = skills.filter(s => s.status === 'ready');
    } else if (_statusFilter === 'needs-setup') {
      skills = skills.filter(s => s.status === 'needs_setup');
    } else if (_statusFilter === 'not-declared') {
      skills = skills.filter(s => s.status === 'not_declared');
    }

    if (skills.length === 0) {
      const msg = _filterText
        ? `No skills match <strong>${_esc(_filterText)}</strong>.`
        : _statusFilter === 'ready'
          ? 'No skills are ready. Install dependencies to enable them.'
          : _statusFilter === 'needs-setup'
            ? 'No skills currently need setup.'
            : _statusFilter === 'not-declared'
              ? 'No skills without declared dependencies.'
              : 'No skills installed.';
      wrap.innerHTML = `<div class="state">
        <div class="state-icon">${icons.skills()}</div>
        <p class="state-text">${msg}</p>
      </div>`;
      return;
    }

    const _rank = (s) => {
      if (s.status === 'ready') return 0;
      if (s.status === 'not_declared') return 1;
      return 2;
    };

    const groups = {};
    skills.forEach(s => {
      const l = s.layer || 'extra';
      (groups[l] = groups[l] || []).push(s);
    });

    const _sortByReady = (list) => list.sort((a, b) => {
      const ra = _rank(a);
      const rb = _rank(b);
      if (ra !== rb) return ra - rb;
      return (a.name || '').localeCompare(b.name || '');
    });
    Object.values(groups).forEach(_sortByReady);

    let html = '';
    _LAYER_ORDER.forEach(layer => {
      const list = groups[layer];
      if (!list || list.length === 0) return;
      html += `<details class="sk-group" open>
        <summary class="sk-group__head">
          <span class="sk-group__caret">▾</span>
          <span class="sk-group__label">${_esc(_layerLabel(layer))}</span>
          <span class="sk-group__count">${list.length}</span>
          <span class="sk-group__meta">${_esc(_layerHelp(layer))}</span>
        </summary>
        <div class="sk-grid">
          ${list.map(_renderCard).join('')}
        </div>
      </details>`;
    });

    wrap.innerHTML = html;
  }

  function _renderCard(skill) {
    const status = skill.status || (skill.eligible ? 'ready' : 'needs_setup');
    let dotCls;
    if (status === 'ready') dotCls = 'is-ready';
    else if (status === 'needs_setup') dotCls = 'is-needs';
    else dotCls = 'is-unverified';

    const dotTitle = skill.status_detail || (skill.eligible ? 'Ready' : 'Needs setup');
    const emoji = skill.emoji ? `<span class="sk-card__emoji">${_esc(skill.emoji)}</span>` : '';
    const desc = skill.description || '';
    return `<button type="button" class="sk-card" data-skill-card="${_esc(skill.name)}" title="${_esc(skill.name + (desc ? ': ' + desc : ''))}">
      <div class="sk-card__head">
        <span class="sk-card__dot ${dotCls}" title="${_esc(dotTitle)}"></span>
        ${emoji}
        <span class="sk-card__name">${_esc(skill.name)}</span>
      </div>
      <p class="sk-card__desc" title="${_esc(desc)}">${_esc(desc)}</p>
    </button>`;
  }

  // ── Community / Bankr browse ───────────────────────────────────────────
  // A "group" is bankr (source=bankr) or community (all non-bankr sources).

  async function _browse(group, query) {
    if (!_el) return;
    _registryLoading[group] = true;
    _renderRegistry(group); // shows loading
    try {
      const params = { query: (query || '').trim(), limit: 200 };
      if (group === 'bankr') params.source = 'bankr';
      const data = await _rpc.call('skills.search', params);
      let results = data.results || [];
      if (group === 'community') results = results.filter(r => r.source !== 'bankr');
      _registryCache[group] = results;
    } catch (err) {
      _registryCache[group] = { error: err.message };
    } finally {
      _registryLoading[group] = false;
      _renderRegistry(group);
    }
  }

  function _categoriesFor(list) {
    const counts = {};
    list.forEach(r => { const c = r.category || 'other'; counts[c] = (counts[c] || 0) + 1; });
    return counts;
  }

  function _renderRegistry(group) {
    if (!_el) return;
    const wrap = _el.querySelector(`[data-results="${group}"]`);
    const chipsWrap = _el.querySelector(`[data-chips="${group}"]`);
    if (!wrap) return;

    if (_registryLoading[group]) {
      if (chipsWrap) chipsWrap.innerHTML = '';
      wrap.innerHTML = `<div class="sk-registry__loading"><span class="sk-spinner"></span> ${group === 'bankr' ? 'Loading Bankr catalog…' : 'Loading community catalog…'}</div>`;
      return;
    }

    const cache = _registryCache[group];
    if (cache && cache.error) {
      if (chipsWrap) chipsWrap.innerHTML = '';
      wrap.innerHTML = `<div class="sk-error">Failed to load: ${_esc(cache.error)}</div>`;
      return;
    }
    const all = Array.isArray(cache) ? cache : [];

    // Category chips (only when categories are meaningful — Bankr provides them).
    if (chipsWrap) {
      const counts = _categoriesFor(all);
      const hasCats = Object.keys(counts).some(c => c && c !== 'other') || Object.keys(counts).length > 1;
      if (hasCats && all.length) {
        const cats = ['all', ...Object.keys(counts).sort((a, b) => counts[b] - counts[a])];
        chipsWrap.innerHTML = cats.map(c => {
          const active = _catFilter[group] === c;
          const label = _CAT_LABEL[c] || c;
          const count = c === 'all' ? all.length : counts[c];
          return `<button type="button" class="sk-chip-btn${active ? ' is-active' : ''}" data-cat-chip="${_esc(c)}" data-group="${group}">${_esc(label)} <span class="sk-chip-btn__count">${count}</span></button>`;
        }).join('');
      } else {
        chipsWrap.innerHTML = '';
      }
    }

    // Apply text + category filters.
    const q = (_registryQuery[group] || '').trim().toLowerCase();
    const cat = _catFilter[group] || 'all';
    let items = all;
    if (cat !== 'all') items = items.filter(r => (r.category || 'other') === cat);
    if (q) {
      items = items.filter(r =>
        (r.name || '').toLowerCase().includes(q) ||
        (r.provider || '').toLowerCase().includes(q) ||
        (r.description || '').toLowerCase().includes(q)
      );
    }

    if (items.length === 0) {
      const msg = q
        ? `No skills match <strong>${_esc(q)}</strong>.`
        : (group === 'bankr' ? 'No Bankr skills available right now.' : 'No community skills available right now.');
      wrap.innerHTML = `<div class="sk-registry__hint"><p>${msg}</p></div>`;
      return;
    }

    wrap.innerHTML = `<div class="sk-grid sk-grid--registry">${items.map(r => _renderRegistryCard(r, group)).join('')}</div>`;
  }

  function _renderRegistryCard(r, group) {
    const badge = r.logo
      ? `<img class="sk-rcard__logo" src="${_esc(r.logo)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'sk-rcard__logo sk-rcard__logo--initials',textContent:'${_esc(_initials(r.provider || r.name))}'}))" />`
      : `<span class="sk-rcard__logo sk-rcard__logo--initials">${_esc(_initials(r.provider || r.name))}</span>`;
    const cat = r.category && r.category !== 'other'
      ? `<span class="sk-rcard__cat">${_esc(_CAT_LABEL[r.category] || r.category)}</span>` : '';
    const desc = r.description || '';
    const key = r.identifier || r.name;
    const action = r.installed
      ? `<span class="sk-chip sk-chip--ok">✓ Installed</span>`
      : `<button class="btn btn--primary btn--sm" data-install="${_esc(key)}" data-source="${_esc(r.source || 'clawhub')}">Install</button>`;
    return `<div class="sk-rcard" data-registry-card="${_esc(key)}" data-group="${group}" role="button" tabindex="0">
      <div class="sk-rcard__head">
        ${badge}
        <div class="sk-rcard__titles">
          <span class="sk-rcard__name">${_esc(r.name)}</span>
          <span class="sk-rcard__provider">${_esc(r.provider || r.source || '')}</span>
        </div>
        ${cat}
      </div>
      <p class="sk-rcard__desc">${_esc(desc || 'View details →')}</p>
      <div class="sk-rcard__foot">
        <span class="sk-rcard__src sk-mono">${_esc(r.source || '')}</span>
        ${action}
      </div>
    </div>`;
  }

  function _openRegistryDialog(r) {
    const dlg = _el.querySelector('#skill-detail-dialog');
    const body = _el.querySelector('#skill-detail-body');
    if (!dlg || !body) return;

    const badge = r.logo
      ? `<img class="sk-dialog__logo" src="${_esc(r.logo)}" alt="" onerror="this.remove()" />`
      : `<span class="sk-dialog__logo sk-dialog__logo--initials">${_esc(_initials(r.provider || r.name))}</span>`;
    const trustCls = r.trust_level === 'trusted' ? 'sk-chip--ok' : 'sk-chip--warn';
    const chips = [
      `<span class="sk-chip ${trustCls}">${_esc(r.trust_level || 'community')}</span>`,
      r.category && r.category !== 'other' ? `<span class="sk-chip">${_esc(_CAT_LABEL[r.category] || r.category)}</span>` : '',
      `<span class="sk-chip sk-mono">${_esc(r.source || '')}</span>`,
    ].join(' ');

    const descHtml = r.description
      ? `<p class="sk-dialog__desc">${_esc(r.description)}</p>`
      : `<p class="sk-dialog__desc sk-dim">Description loads after install (from the skill's SKILL.md).</p>`;

    let setupHtml = '';
    if (Array.isArray(r.setup) && r.setup.length) {
      setupHtml = `<div class="sk-dialog__section">
        <div class="sk-dialog__section-title">Setup</div>
        <ol class="sk-dialog__setup">${r.setup.map(s => `<li>${_esc(s)}</li>`).join('')}</ol>
      </div>`;
    }

    let demoHtml = '';
    if (r.demo && r.demo.code) {
      const lang = r.demo.language ? `<span class="sk-dialog__demo-lang sk-mono">${_esc(r.demo.language)}</span>` : '';
      const title = r.demo.title ? `<span class="sk-dialog__demo-title sk-mono">${_esc(r.demo.title)}</span>` : '';
      demoHtml = `<div class="sk-dialog__section">
        <div class="sk-dialog__section-title">Demo ${title} ${lang}</div>
        <pre class="sk-dialog__code"><code>${_esc(r.demo.code)}</code></pre>
      </div>`;
    }

    const homepage = r.homepage
      ? `<a href="${_esc(r.homepage)}" target="_blank" rel="noopener" class="sk-dialog__link">Source ↗</a>`
      : '';
    const key = r.identifier || r.name;
    const actionBtn = r.installed
      ? `<span class="sk-chip sk-chip--ok">✓ Installed</span>`
      : `<button class="btn btn--primary" data-install="${_esc(key)}" data-source="${_esc(r.source || 'clawhub')}">Install skill</button>`;

    body.innerHTML = `
      <header class="sk-dialog__head">
        <div class="sk-dialog__head-left">
          ${badge}
          <div>
            <strong class="sk-dialog__name">${_esc(r.name)}</strong>
            <div class="sk-dialog__provider">${_esc(r.provider || '')}</div>
          </div>
        </div>
        <button type="button" class="sk-iconbtn" id="skill-dialog-close" aria-label="Close">${icons.x()}</button>
      </header>
      <section class="sk-dialog__body">
        <div class="sk-dialog__chips">${chips}</div>
        ${descHtml}
        ${setupHtml}
        ${demoHtml}
        ${homepage ? `<div class="sk-dialog__section">${homepage}</div>` : ''}
      </section>
      <footer class="sk-dialog__foot">
        <small class="sk-dim sk-mono sk-dialog__path">${_esc(key)}</small>
        ${actionBtn}
      </footer>`;

    const closeBtn = body.querySelector('#skill-dialog-close');
    if (closeBtn) closeBtn.addEventListener('click', () => dlg.close(), { once: true });
    if (dlg.open) dlg.close();
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
  }

  // ── Installed skill detail dialog ──────────────────────────────────────

  function _renderRequirements(requirements) {
    const items = requirements && Array.isArray(requirements.items) ? requirements.items : [];
    if (!items.length) return '';
    const rows = items.map(item => {
      const missing = [];
      (item.missing_bins || []).forEach(b => missing.push(`<code>${_esc(b)}</code>`));
      (item.missing_env || []).forEach(e => missing.push(`<code>${_esc(e)}</code>`));
      const requires = [];
      (item.requires_bins || []).forEach(b => requires.push(_esc(b)));
      if ((item.requires_any_bins || []).length) {
        requires.push(`one of ${(item.requires_any_bins || []).map(_esc).join(' / ')}`);
      }
      (item.requires_env || []).forEach(e => requires.push(`${_esc(e)} env`));
      const status = item.status || 'not_declared';
      const statusLabel = status === 'ready' ? 'ready'
        : status === 'needs_setup' ? 'needs setup'
          : status === 'missing_skill' ? 'missing skill'
            : 'no deps declared';
      const statusClass = status === 'ready' ? 'sk-chip--ok'
        : status === 'needs_setup' || status === 'missing_skill' ? 'sk-chip--warn'
          : 'sk-chip--unverified';
      const detail = missing.length
        ? `Missing ${missing.join(', ')}`
        : requires.length ? requires.join(', ') : 'No declared dependencies';
      return `<div class="sk-dialog__req-row">
        <span class="sk-dialog__req-name">${_esc(item.name || 'unknown')}</span>
        <span class="sk-chip ${statusClass}">${statusLabel}</span>
        <span class="sk-dialog__req-detail">${detail}</span>
      </div>`;
    }).join('');
    return `<div class="sk-dialog__section">
      <div class="sk-dialog__section-title">Requirements</div>
      <div class="sk-dialog__requirements">${rows}</div>
    </div>`;
  }

  function _openSkillDialog(skill) {
    const dlg = _el.querySelector('#skill-detail-dialog');
    const body = _el.querySelector('#skill-detail-body');
    if (!dlg || !body) return;

    const statusDetail = skill.status_detail || '';
    const status = skill.status || (skill.eligible ? 'ready' : 'needs_setup');
    let statusChip;
    if (status === 'ready') {
      statusChip = `<span class="sk-chip sk-chip--ok" title="${_esc(statusDetail)}">✓ ready</span>`;
    } else if (status === 'not_declared') {
      statusChip = `<span class="sk-chip sk-chip--unverified" title="${_esc(statusDetail)}">no deps declared</span>`;
    } else {
      statusChip = `<span class="sk-chip sk-chip--warn" title="${_esc(statusDetail)}">needs deps</span>`;
    }
    const layerChip = `<span class="sk-chip" title="${_esc(_layerHelp(skill.layer))}">${_esc(_layerLabel(skill.layer))}</span>`;

    let missingHtml = '';
    if (status === 'needs_setup') {
      const missing = [];
      (skill.missing_bins || []).forEach(b => missing.push(`<li><code>${_esc(b)}</code> <span class="sk-dim">binary</span></li>`));
      (skill.missing_env || []).forEach(e => missing.push(`<li><code>${_esc(e)}</code> <span class="sk-dim">env var</span></li>`));
      if (missing.length) {
        missingHtml = `<div class="sk-dialog__section">
          <div class="sk-dialog__section-title">Missing</div>
          <ul class="sk-dialog__missing">${missing.join('')}</ul>
        </div>`;
      }
    }

    const requirementsHtml = _renderRequirements(skill.requirements);

    let installHtml = '';
    const hasMissingBins = (skill.missing_bins || []).length > 0;
    const installs = hasMissingBins ? (skill.install || []) : [];
    if (installs.length) {
      const rows = installs.map(i => {
        const bins = (i.bins || []).length ? `<span class="sk-dim"> (${(i.bins || []).map(_esc).join(', ')})</span>` : '';
        const label = i.label || `Install via ${i.kind}`;
        return `<div class="sk-dialog__install-row">
          <span>${_esc(label)}${bins}</span>
          <button class="btn btn--primary btn--sm" data-install-deps-name="${_esc(skill.name)}" data-install-deps-id="${_esc(i.id)}">Install via ${_esc(i.kind)}</button>
        </div>`;
      }).join('');
      installHtml = `<div class="sk-dialog__section">
        <div class="sk-dialog__section-title">Install</div>
        ${rows}
      </div>`;
    }

    const homepage = skill.homepage
      ? `<a href="${_esc(skill.homepage)}" target="_blank" rel="noopener" class="sk-dialog__link">Homepage ↗</a>`
      : '';

    const footer = skill.file_path
      ? `<small class="sk-dim sk-dialog__path">${_esc(skill.file_path)}</small>`
      : '';

    const removeBtn = skill.layer === 'managed'
      ? `<button class="btn btn--sm" data-uninstall="${_esc(skill.name)}">Remove</button>`
      : '';

    body.innerHTML = `
      <header class="sk-dialog__head">
        <div class="sk-dialog__head-left">
          ${skill.emoji ? `<span class="sk-dialog__emoji">${_esc(skill.emoji)}</span>` : ''}
          <strong class="sk-dialog__name">${_esc(skill.name)}</strong>
          <div class="sk-dialog__chips">${layerChip} ${statusChip}</div>
        </div>
        <button type="button" class="sk-iconbtn" id="skill-dialog-close" aria-label="Close">${icons.x()}</button>
      </header>
      <section class="sk-dialog__body">
        <p class="sk-dialog__desc">${_esc(skill.description || '')}</p>
        ${requirementsHtml}
        ${missingHtml}
        ${installHtml}
        ${homepage ? `<div class="sk-dialog__section">${homepage}</div>` : ''}
      </section>
      <footer class="sk-dialog__foot">
        ${footer}
        ${removeBtn}
      </footer>`;

    const closeBtn = body.querySelector('#skill-dialog-close');
    if (closeBtn) closeBtn.addEventListener('click', () => dlg.close(), { once: true });

    if (dlg.open) dlg.close();
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
  }

  async function _installDeps(name, installId, btn) {
    if (!_rpc || !name || !installId) return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Installing…';
    try {
      const res = await _rpc.call('skills.deps.install', { name, install_id: installId });
      if (res.success) {
        btn.textContent = '✓ Installed';
        UI.toast(res.message || 'Installed', 'ok');
      } else {
        btn.textContent = 'Failed';
        btn.disabled = false;
        UI.toast(res.message || 'Install failed', 'err');
      }
      const still = res.missing_still || {};
      const stillMissing = (still.bins || []).length + (still.env || []).length;
      if (stillMissing === 0) {
        setTimeout(() => {
          const dlg = _el && _el.querySelector('#skill-detail-dialog');
          if (dlg && dlg.open) dlg.close();
        }, 600);
      }
      await _loadData();
    } catch (err) {
      btn.textContent = originalText;
      btn.disabled = false;
      UI.toast(err.message, 'err');
    }
  }

  async function _installSkill(identifier, source, btn) {
    if (!_rpc) return;
    btn.disabled = true;
    btn.textContent = 'Installing…';
    try {
      const res = await _rpc.call('skills.install', { identifier, source });
      if (res.success) {
        btn.textContent = '✓ Installed';
        btn.classList.remove('btn--primary');
        // Invalidate registry caches so the "installed" badge refreshes.
        _registryCache = { bankr: null, community: null };
        _loadData();
      } else {
        btn.textContent = 'Failed';
        btn.disabled = false;
        UI.toast(res.message || 'Install failed', 'err');
      }
    } catch (err) {
      btn.textContent = 'Error';
      btn.disabled = false;
      UI.toast(err.message, 'err');
    }
  }

  async function _uninstallSkill(name, btn) {
    if (!_rpc) return;
    btn.disabled = true;
    btn.textContent = 'Removing…';
    try {
      const res = await _rpc.call('skills.uninstall', { name });
      if (res.success) { _registryCache = { bankr: null, community: null }; _loadData(); }
      else { btn.textContent = 'Failed'; UI.toast(res.message || 'Uninstall failed', 'err'); }
    } catch (err) { btn.textContent = 'Error'; UI.toast(err.message, 'err'); }
  }

  // ── helpers ────────────────────────────────────────────────────────────

  function _debounce(fn, ms) {
    let t = null;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function _initials(text) {
    const words = (text || '').trim().split(/\s+/).filter(Boolean);
    if (!words.length) return '?';
    return (words[0][0] + (words[1] ? words[1][0] : '')).toUpperCase();
  }

  function _basePath() {
    return document.getElementById('agentos-data')?.dataset.basePath || '';
  }

  function _bankrFallbackGlyph(size) {
    // Drawn "B" mark, used if the brand SVG asset fails to load.
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M9 8h4a2 2 0 0 1 0 4H9zm0 4h4.5a2 2 0 0 1 0 4H9z"/></svg>`;
  }

  function _bankrGlyph(size = 16) {
    // Official Bankr brand mark (served locally); falls back to a drawn glyph.
    const src = `${_basePath()}/static/img/bankr-symbol.svg`;
    const fallback = _bankrFallbackGlyph(size).replace(/"/g, '&quot;');
    return `<img class="sk-bankr-logo" src="${src}" alt="Bankr" width="${size}" height="${size}" onerror="this.outerHTML='${fallback}'" />`;
  }

  function _esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _layerLabel(layer) {
    return _LAYER_LABEL[layer] || layer || 'Unknown';
  }

  function _layerHelp(layer) {
    return _LAYER_HELP[layer] || 'Configured local skill directory.';
  }

  return { render, destroy };
})();

window.SkillsView = SkillsView;
