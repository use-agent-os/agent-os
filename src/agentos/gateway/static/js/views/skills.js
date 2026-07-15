/** AgentOS Web UI — Skills Management view. */

const SkillsView = (() => {
  // Show/hide the Bankr partner tab in the Skills view. Set to true to bring
  // the tab back — the BankrSource backend (browse/search/install) stays wired
  // either way, so Bankr skills remain reachable via the Community tab / CLI.
  const _SHOW_BANKR = false;

  let _el = null;
  let _rpc = null;
  let _unsubs = [];
  let _intervals = [];
  let _allSkills = [];
  let _filterText = '';
  let _statusFilter = 'all';
  let _activeTab = 'installed';

  // Community/Bankr browse state. _registryCache holds the empty-query
  // snapshot per group (array | null); fetch failures live in _registryError
  // so a failed load never poisons the cache — a null cache means "retry on
  // next tab entry".
  let _registryCache = { bankr: null, community: null };
  let _registryError = { bankr: '', community: '' };
  let _registryLoading = { bankr: false, community: false };
  let _catFilter = { bankr: 'all', community: 'all' };
  let _registryQuery = { bankr: '', community: '' };
  // Server-side results for the current non-empty community query (the
  // community snapshot only covers each source's first page, so typed queries
  // must reach the server). _communitySeq drops stale async responses.
  let _communityResults = null;
  let _communitySeq = 0;
  // Identifiers armed for force-install after a security-scan block. Kept in
  // view state (not on the button) so re-renders don't disarm the override.
  const _forceArmed = new Set();

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
    const cssVersion = data?.dataset.version || '';
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = `${_basePath()}/static/css/views/skills.css${cssVersion ? '?v=' + encodeURIComponent(cssVersion) : ''}`;
    link.dataset.viewCss = 'skills';
    document.head.appendChild(link);
  }

  function render(el) {
    _el = el;
    _rpc = App.getRpc();
    _registryCache = { bankr: null, community: null };
    _registryError = { bankr: '', community: '' };
    _registryLoading = { bankr: false, community: false };
    _catFilter = { bankr: 'all', community: 'all' };
    _registryQuery = { bankr: '', community: '' };
    _communityResults = null;
    _communitySeq++;
    _forceArmed.clear();
    _activeTab = 'installed';
    _ensureCss();

    _el.innerHTML = `
      <div class="sk-stage">
        <header class="sk-hero">
          <div class="sk-hero__top">
            <div class="sk-hero__intro">
              <span class="sk-hero__eyebrow">${icons.skills()}<span>Control · Skills</span></span>
              <h2 class="sk-hero__title">Skills</h2>
              <p class="sk-hero__subtitle">Composable agent capabilities — bundled packs, partner catalogs, and the wider community.</p>
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
          ${_SHOW_BANKR ? `<button class="sk-tab sk-tab--bankr" data-tab="bankr" aria-pressed="false">${_bankrGlyph()}<span>Bankr</span></button>` : ''}
          <button class="sk-tab sk-tab--robinhood" data-tab="robinhood" aria-pressed="false">${_robinhoodGlyph()}<span>Robinhood</span></button>
          <button class="sk-tab" data-tab="community" aria-pressed="false">${icons.download()}<span>Community</span></button>
        </div>

        <div id="skills-tab-installed" class="sk-panel">
          <div id="skills-installed-wrap"></div>
        </div>

        ${_SHOW_BANKR ? `
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
        </div>` : ''}

        <div id="skills-tab-robinhood" class="sk-panel" hidden>
          <div class="sk-partner sk-partner--robinhood">
            <div class="sk-partner__mark">${_robinhoodGlyph(48)}</div>
            <div class="sk-partner__text">
              <div class="sk-partner__name">Robinhood partner catalog</div>
              <p class="sk-partner__desc">Trade stocks, ETFs, and crypto through Robinhood — tokenized equities, spot &amp; leverage, and Robinhood Chain skills.</p>
            </div>
            <a class="sk-partner__link" href="https://robinhood.com" target="_blank" rel="noopener">robinhood.com ↗</a>
          </div>
          <div class="sk-browse" data-group="robinhood">
            <div class="sk-browse__results" id="skills-robinhood-wrap"></div>
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
      if (_activeTab === 'bankr' || _activeTab === 'community') {
        // Always refresh the full snapshot; the typed query is re-applied at
        // render time, so a query-scoped fetch never poisons the cache.
        _registryCache[_activeTab] = null;
        _registryError[_activeTab] = '';
        _browse(_activeTab);
        return;
      }
      // Installed and Robinhood tabs both render from skills.list data.
      _loadData();
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
        if (group === 'community') {
          const q = input.value.trim();
          if (q) { _searchCommunity(q); return; }
          _communityResults = null;
          _communitySeq++; // drop any in-flight search response
        }
        _renderRegistryResults(group);
      }, 250));
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

    // Keyboard activation for registry cards (role="button" divs).
    _el.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      if (e.target.closest('button, a, input')) return; // native elements handle themselves
      const card = e.target.closest('[data-registry-card]');
      if (card) { e.preventDefault(); card.click(); }
    });

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
        const force = installBtn.dataset.force === '1';
        _installSkill(installBtn.dataset.install, installBtn.dataset.source || 'clawhub', installBtn, force);
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
        const item = _registryItems(group).find(
          r => (r.identifier || r.name) === regCard.dataset.registryCard
        );
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
    // A failed load leaves the cache null, so re-entering the tab retries.
    if ((tab === 'bankr' || tab === 'community') && _registryCache[tab] === null && !_registryLoading[tab]) {
      _browse(tab);
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
      _renderRobinhood();
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

  // ── Robinhood tab (installed Robinhood-family skills) ──────────────────

  function _isRobinhoodSkill(skill) {
    // Partner grouping is a brand surface, not just a filter — restrict it to
    // bundled skills so a user-installed community skill can't wear the
    // partner banner by naming itself robinhood-* or claiming the homepage.
    if (skill.layer !== 'bundled') return false;
    const name = (skill.name || '').toLowerCase();
    const home = (skill.homepage || '').toLowerCase();
    return name.startsWith('robinhood') || home.includes('robinhood.com');
  }

  function _renderRobinhood() {
    if (!_el) return;
    const wrap = _el.querySelector('#skills-robinhood-wrap');
    if (!wrap) return;

    const skills = _allSkills
      .filter(_isRobinhoodSkill)
      .sort((a, b) => (a.name || '').localeCompare(b.name || ''));

    if (skills.length === 0) {
      wrap.innerHTML = `<div class="sk-empty">
        <div class="sk-empty__mark">${_robinhoodGlyph(40)}</div>
        <p class="sk-empty__title">Robinhood skills are on the way</p>
        <p class="sk-empty__hint">No Robinhood skills installed yet. Check back soon, or browse the Bankr &amp; Community catalogs in the meantime.</p>
      </div>`;
      return;
    }
    wrap.innerHTML = `<div class="sk-grid">${skills.map(_renderCard).join('')}</div>`;
  }

  // ── Community / Bankr browse ───────────────────────────────────────────
  // A "group" is bankr (source=bankr) or community. Community excludes Bankr
  // only while the dedicated Bankr tab is showing; when that tab is hidden,
  // Bankr skills fall through into Community so they stay reachable.
  function _communityFilter(results) {
    return _SHOW_BANKR ? results.filter(r => r.source !== 'bankr') : results;
  }

  async function _browse(group) {
    if (!_el) return;
    _registryLoading[group] = true;
    _registryError[group] = '';
    _renderRegistry(group); // shows loading
    try {
      const params = { query: '', limit: 500 };
      if (group === 'bankr') params.source = 'bankr';
      const data = await _rpc.call('skills.search', params);
      let results = data.results || [];
      if (group === 'community') results = _communityFilter(results);
      _registryCache[group] = results;
    } catch (err) {
      // Leave the cache null so the next tab entry retries automatically.
      _registryError[group] = err.message;
    } finally {
      _registryLoading[group] = false;
      _renderRegistry(group);
    }
  }

  async function _searchCommunity(query) {
    if (!_el || !_rpc) return;
    const seq = ++_communitySeq;
    const wrap = _el.querySelector('[data-results="community"]');
    if (wrap) {
      wrap.innerHTML = `<div class="sk-registry__loading"><span class="sk-spinner"></span> Searching community skills…</div>`;
    }
    let results = [];
    try {
      const data = await _rpc.call('skills.search', { query, limit: 100 });
      results = _communityFilter(data.results || []);
    } catch (err) {
      results = [];
    }
    if (seq !== _communitySeq) return; // a newer query superseded this one
    _communityResults = results;
    _renderRegistryResults('community');
  }

  /** The base item list a group renders from (before category/text filters). */
  function _registryItems(group) {
    if (
      group === 'community' &&
      (_registryQuery.community || '').trim() &&
      Array.isArray(_communityResults)
    ) {
      return _communityResults;
    }
    const cache = _registryCache[group];
    return Array.isArray(cache) ? cache : [];
  }

  function _categoriesFor(list) {
    const counts = {};
    list.forEach(r => { const c = r.category || 'other'; counts[c] = (counts[c] || 0) + 1; });
    return counts;
  }

  /** Chips derive from the full snapshot only — they never change on keystrokes. */
  function _renderChips(group) {
    if (!_el) return;
    const chipsWrap = _el.querySelector(`[data-chips="${group}"]`);
    if (!chipsWrap) return;

    const cache = _registryCache[group];
    const all = Array.isArray(cache) ? cache : [];
    const counts = _categoriesFor(all);
    const hasCats = Object.keys(counts).some(c => c && c !== 'other') || Object.keys(counts).length > 1;
    if (!hasCats || !all.length) {
      chipsWrap.innerHTML = '';
      return;
    }
    const cats = ['all', ...Object.keys(counts).sort((a, b) => counts[b] - counts[a])];
    chipsWrap.innerHTML = cats.map(c => {
      const active = _catFilter[group] === c;
      const label = _CAT_LABEL[c] || c;
      const count = c === 'all' ? all.length : counts[c];
      return `<button type="button" class="sk-chip-btn${active ? ' is-active' : ''}" data-cat-chip="${_esc(c)}" data-group="${group}">${_esc(label)} <span class="sk-chip-btn__count">${count}</span></button>`;
    }).join('');
  }

  function _renderRegistry(group) {
    _renderChips(group);
    _renderRegistryResults(group);
  }

  function _renderRegistryResults(group) {
    if (!_el) return;
    const wrap = _el.querySelector(`[data-results="${group}"]`);
    if (!wrap) return;

    if (_registryLoading[group]) {
      wrap.innerHTML = `<div class="sk-registry__loading"><span class="sk-spinner"></span> ${group === 'bankr' ? 'Loading Bankr catalog…' : 'Loading community catalog…'}</div>`;
      return;
    }

    if (_registryError[group]) {
      wrap.innerHTML = `<div class="sk-error">Failed to load: ${_esc(_registryError[group])}<br><span class="sk-dim">Re-open the tab or press Refresh to retry.</span></div>`;
      return;
    }

    // Apply text + category filters.
    const q = (_registryQuery[group] || '').trim().toLowerCase();
    const cat = _catFilter[group] || 'all';
    let items = _registryItems(group);
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

  function _installAction(r, { small = true } = {}) {
    if (r.installed) return `<span class="sk-chip sk-chip--ok">✓ Installed</span>`;
    const key = r.identifier || r.name;
    const sm = small ? ' btn--sm' : '';
    if (_forceArmed.has(key)) {
      return `<button class="btn btn--danger${sm}" data-install="${_esc(key)}" data-source="${_esc(r.source || 'clawhub')}" data-force="1">⚠ Force install</button>`;
    }
    return `<button class="btn btn--primary${sm}" data-install="${_esc(key)}" data-source="${_esc(r.source || 'clawhub')}">${small ? 'Install' : 'Install skill'}</button>`;
  }

  function _logoBadge(r, cls) {
    // The initials fallback is rendered as a hidden sibling and revealed by a
    // static onerror handler — never interpolate data into inline JS.
    const initials = _esc(_initials(r.provider || r.name));
    const logoUrl = _safeUrl(r.logo);
    if (!logoUrl) return `<span class="${cls} ${cls}--initials">${initials}</span>`;
    return `<img class="${cls}" src="${_esc(logoUrl)}" alt="" loading="lazy" onerror="this.style.display='none';if(this.nextElementSibling)this.nextElementSibling.style.display='inline-flex'" /><span class="${cls} ${cls}--initials" style="display:none">${initials}</span>`;
  }

  function _renderRegistryCard(r, group) {
    const badge = _logoBadge(r, 'sk-rcard__logo');
    const cat = r.category && r.category !== 'other'
      ? `<span class="sk-rcard__cat">${_esc(_CAT_LABEL[r.category] || r.category)}</span>` : '';
    const desc = r.description || '';
    const key = r.identifier || r.name;
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
        ${_installAction(r)}
      </div>
    </div>`;
  }

  function _openRegistryDialog(r) {
    const badge = _logoBadge(r, 'sk-dialog__logo');
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

    const homepageUrl = _safeUrl(r.homepage);
    const homepage = homepageUrl
      ? `<a href="${_esc(homepageUrl)}" target="_blank" rel="noopener" class="sk-dialog__link">Source ↗</a>`
      : '';
    const key = r.identifier || r.name;

    _openDialog(`
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
        ${_installAction(r, { small: false })}
      </footer>`);
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

    const homepageUrl = _safeUrl(skill.homepage);
    const homepage = homepageUrl
      ? `<a href="${_esc(homepageUrl)}" target="_blank" rel="noopener" class="sk-dialog__link">Homepage ↗</a>`
      : '';

    const footer = skill.file_path
      ? `<small class="sk-dim sk-dialog__path">${_esc(skill.file_path)}</small>`
      : '';

    const removeBtn = skill.layer === 'managed'
      ? `<button class="btn btn--sm" data-uninstall="${_esc(skill.name)}">Remove</button>`
      : '';

    _openDialog(`
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
      </footer>`);
  }

  /** Fill the shared detail dialog, wire its close button, and show it. */
  function _openDialog(html) {
    const dlg = _el.querySelector('#skill-detail-dialog');
    const body = _el.querySelector('#skill-detail-body');
    if (!dlg || !body) return;
    body.innerHTML = html;
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

  /** Flip `installed` on cached registry rows matching by identifier or name. */
  function _markInstalled(identifier, name, installed) {
    const flip = (list) => {
      if (!Array.isArray(list)) return;
      list.forEach(r => {
        const key = r.identifier || r.name;
        if ((identifier && key === identifier) || (name && r.name === name)) {
          r.installed = installed;
        }
      });
    };
    flip(_registryCache.bankr);
    flip(_registryCache.community);
    flip(_communityResults);
  }

  async function _installSkill(identifier, source, btn, force = false) {
    if (!_rpc) return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = force ? 'Force installing…' : 'Installing…';
    try {
      const res = await _rpc.call('skills.install', { identifier, source, force });
      if (res.success) {
        btn.textContent = '✓ Installed';
        btn.classList.remove('btn--primary');
        btn.classList.remove('btn--danger');
        _forceArmed.delete(identifier);
        // Mark the item installed in-place so the badge flips without
        // discarding the browsed catalog.
        _markInstalled(identifier, res.name, true);
        _loadData();
        return;
      }

      // A "dangerous" security verdict is a deliberate block, not a crash.
      // Explain it and arm an explicit force-install override. The armed
      // state lives in _forceArmed so re-renders keep the button armed.
      const blocked = res.scan_verdict === 'dangerous';
      const n = (res.scan_findings || []).length;
      btn.disabled = false;
      if (blocked && !force) {
        _forceArmed.add(identifier);
        btn.textContent = '⚠ Force install';
        btn.classList.add('btn--danger');
        btn.classList.remove('btn--primary');
        btn.dataset.force = '1';
        UI.toast(
          `Security scan flagged ${res.name || 'this skill'}${n ? ' (' + n + ' finding' + (n === 1 ? '' : 's') + ')' : ''}. Click again to install anyway.`,
          'err'
        );
      } else {
        btn.textContent = 'Failed';
        UI.toast(res.message || 'Install failed', 'err');
      }
    } catch (err) {
      btn.textContent = originalText;
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
      if (res.success) {
        // Flip the badge in-place; the browsed catalogs stay valid.
        _markInstalled('', name, false);
        _loadData();
      } else { btn.textContent = 'Failed'; UI.toast(res.message || 'Uninstall failed', 'err'); }
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

  /** Allow only http(s) URLs from remote catalogs — never javascript: etc. */
  function _safeUrl(url) {
    const u = String(url || '').trim();
    return /^https?:\/\//i.test(u) ? u : '';
  }

  // Partner brand marks: local asset with a drawn-glyph fallback. Fallback
  // SVGs are static strings — no data is interpolated into the inline JS.
  const _BRANDS = {
    bankr: {
      asset: 'bankr-symbol.svg',
      alt: 'Bankr',
      cls: 'sk-bankr-logo',
      fallback: (s) => `<svg xmlns="http://www.w3.org/2000/svg" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M9 8h4a2 2 0 0 1 0 4H9zm0 4h4.5a2 2 0 0 1 0 4H9z"/></svg>`,
    },
    robinhood: {
      asset: 'robinhood-symbol.png',
      alt: 'Robinhood',
      cls: 'sk-robinhood-logo',
      fallback: (s) => `<svg xmlns="http://www.w3.org/2000/svg" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 4C10 5 6 12 6 20"/><path d="M6 20l4-4"/><path d="M18 6c-3 0-7 2-8 6"/></svg>`,
    },
  };

  function _brandGlyph(brand, size = 16) {
    const b = _BRANDS[brand];
    const fallback = b.fallback(size).replace(/"/g, '&quot;');
    return `<img class="${b.cls}" src="${_basePath()}/static/img/${b.asset}" alt="${b.alt}" width="${size}" height="${size}" onerror="this.outerHTML='${fallback}'" />`;
  }

  const _bankrGlyph = (size = 16) => _brandGlyph('bankr', size);
  const _robinhoodGlyph = (size = 16) => _brandGlyph('robinhood', size);

  function _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
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
