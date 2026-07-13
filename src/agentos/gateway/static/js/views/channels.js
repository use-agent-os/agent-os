/** AgentOS Web UI — channel runtime status and Telegram account approvals. */

const ChannelsView = (() => {
  let _el = null;
  let _rpc = null;
  let _unsubs = [];
  let _intervals = [];
  let _channels = [];

  function _ensureCss() {
    if (document.querySelector('link[data-view-css="channels"]')) return;
    const data = document.getElementById('agentos-data');
    const base = data?.dataset.basePath || '';
    const cssVersion = data?.dataset.version || '';
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = `${base}/static/css/views/channels.css${cssVersion ? '?v=' + encodeURIComponent(cssVersion) : ''}`;
    link.dataset.viewCss = 'channels';
    document.head.appendChild(link);
  }

  function render(el) {
    _el = el;
    _rpc = App.getRpc();
    _ensureCss();

    _el.innerHTML = `
      <div class="ch-stage">
        <header class="ch-stage__header">
          <div class="ch-stage__title-block">
            <span class="ch-stage__eyebrow">Control · Channels</span>
            <h2 class="ch-stage__title">Channels</h2>
            <p class="ch-stage__subtitle">Runtime status for configured channels, with account approvals for Telegram bots.</p>
          </div>
          <div class="ch-stage__actions">
            <button class="btn btn--ghost" id="ch-refresh" title="Refresh">
              ${icons.refresh()}<span>Refresh</span>
            </button>
          </div>
        </header>

        <section class="stat-row" id="stat-row"></section>

        <section class="ch-list">
          <div class="ch-list__head">
            <h3 class="ch-list__title" id="ch-list-title">Configured channels</h3>
          </div>
          <div id="ch-cards" class="ch-cards"></div>
        </section>
      </div>`;

    _el.querySelector('#ch-refresh').addEventListener('click', _loadData);

    // Subscribe to real-time channel status events
    const unsub = _rpc.on('channel.status', () => _loadData());
    _unsubs.push(unsub);

    _loadData();

    const id = setInterval(_loadData, 5000);
    _intervals.push(id);
  }

  function destroy() {
    _unsubs.forEach(fn => fn());
    _unsubs = [];
    _intervals.forEach(id => clearInterval(id));
    _intervals = [];
    _channels = [];
    _el = null;
    _rpc = null;
  }

  async function _loadData() {
    if (!_el) return;
    const rpc = _rpc;
    if (!rpc) return;
    await rpc.waitForConnection();
    if (!_el || _rpc !== rpc) return;

    return Promise.all([
      rpc.call('channels.status'),
      rpc.call('channels.access.list').catch(() => ({ channels: [] })),
    ]).then(([data, accessData]) => {
      if (!_el) return;
      const raw = (data.channels || []).filter(c => c && c.configured !== false);
      const accessByName = new Map(
        (accessData.channels || []).map(item => [String(item.name || ''), item])
      );

      // Sort by operator urgency, then surface channels with pending accounts.
      const order = { running: 0, connected: 0, restarting: 1, exhausted: 1, dead: 1, stopped: 2, disabled: 3 };
      _channels = raw.map(item => ({
        ...item,
        access: accessByName.get(String(item.name || '')) || null,
      })).sort((a, b) => {
        const pendingA = Number(a.access?.pending?.length || 0);
        const pendingB = Number(b.access?.pending?.length || 0);
        if (pendingA !== pendingB) return pendingB - pendingA;
        const oa = order[a.status] ?? 1;
        const ob = order[b.status] ?? 1;
        return oa - ob;
      });

      _renderStats();
      _renderCards();
    }).catch(err => UI.toast('Failed to load channels: ' + err.message, 'err'));
  }

  function _renderStats() {
    const wrap = _el && _el.querySelector('#stat-row');
    if (!wrap) return;
    const total = _channels.length;
    const connected = _channels.filter(c => c.status === 'running' || c.status === 'connected').length;
    const attention = _channels.filter(c => _needsAttention(c.status)).length;
    const inactive = total - connected - attention;
    const disabled = _channels.filter(c => c.status === 'disabled').length;
    const restarts = _channels.reduce((acc, c) => acc + (Number(c.restart_attempts) || 0), 0);
    const pendingAccess = _channels.reduce((acc, c) => acc + (c.access?.pending?.length || 0), 0);
    const types = new Set();
    _channels.forEach(c => { if (c.type) types.add(c.type); });

    wrap.innerHTML = `
      <div class="stat stat--hero">
        <div class="stat-label">Total channels</div>
        <div class="stat-value">${total}</div>
        <div class="stat-hint">${types.size} type${types.size === 1 ? '' : 's'}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Connected</div>
        <div class="stat-value">
          ${connected}${connected ? '<span class="dot ok"></span>' : ''}
        </div>
        <div class="stat-hint">${connected ? 'live' : (attention ? `${attention} unhealthy` : 'all idle')}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Inactive</div>
        <div class="stat-value">${inactive}</div>
        <div class="stat-hint">${attention ? `<span class="ch-neg">${attention} need attention</span>` : _inactiveHint(inactive, disabled)}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Restart attempts</div>
        <div class="stat-value mono">${restarts}</div>
        <div class="stat-hint">since gateway start</div>
      </div>
      <div class="stat${pendingAccess ? ' stat--attention' : ''}">
        <div class="stat-label">Chat approvals</div>
        <div class="stat-value mono">${pendingAccess}</div>
        <div class="stat-hint">${pendingAccess ? 'Telegram account requests' : 'no accounts waiting'}</div>
      </div>`;
  }

  function _renderCards() {
    const container = _el && _el.querySelector('#ch-cards');
    const titleEl = _el && _el.querySelector('#ch-list-title');
    if (!container) return;
    if (titleEl) {
      titleEl.innerHTML = _channels.length
        ? `Configured channels <span class="ch-list__count">${_channels.length}</span>`
        : 'Configured channels';
    }

    if (_channels.length === 0) {
      container.innerHTML = `<div class="ch-empty">
        <div class="ch-empty__art" aria-hidden="true">
          <svg viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <radialGradient id="cg2" cx="50%" cy="50%" r="50%">
                <stop offset="0%" stop-color="rgba(204,255,0,0.18)"/>
                <stop offset="60%" stop-color="rgba(204,255,0,0.04)"/>
                <stop offset="100%" stop-color="rgba(204,255,0,0)"/>
              </radialGradient>
            </defs>
            <circle cx="60" cy="60" r="58" fill="url(#cg2)"/>
            <g fill="none" stroke="currentColor" stroke-width="1.4" opacity="0.55">
              <rect x="20" y="40" width="36" height="40" rx="6"/>
              <line x1="28" y1="52" x2="48" y2="52"/>
              <line x1="28" y1="60" x2="44" y2="60"/>
            </g>
            <g fill="none" stroke="var(--accent)" stroke-width="1.6">
              <rect x="64" y="40" width="36" height="40" rx="6"/>
              <line x1="72" y1="52" x2="92" y2="52"/>
              <line x1="72" y1="60" x2="88" y2="60"/>
            </g>
            <g stroke="var(--accent)" stroke-width="1.4" stroke-dasharray="2 4" opacity="0.7">
              <line x1="56" y1="60" x2="64" y2="60"/>
            </g>
          </svg>
        </div>
        <div class="ch-empty__title">No configured channels.</div>
        <p class="ch-empty__msg">Channel provisioning stays in guided setup and the CLI so credentials, dependency extras, webhook URLs, and restart requirements stay explicit.</p>
        <div class="ch-empty__actions">
          <button class="btn btn--primary" id="ch-guided-setup" type="button">${icons.config()}<span>Guided setup</span></button>
        </div>
        <code class="ch-empty__code">agentos onboard configure channels</code>
        <code class="ch-empty__code">agentos channels list</code>
      </div>`;
      _el.querySelector('#ch-guided-setup')?.addEventListener('click', () => Router.navigate('/setup'));
      return;
    }

    container.innerHTML = _channels.map((ch, i) => {
      const name = ch.name || ch.id || 'Unknown';
      const status = ch.status || (ch.connected ? 'connected' : 'stopped');
      const isRunning = status === 'running' || status === 'connected';
      const isDead = status === 'dead';
      const dotCls = isRunning ? 'ok' : isDead ? 'err' : 'off';
      const chipCls = isRunning ? 'chip-ok' : isDead ? 'chip-danger' : '';
      const since = ch.connected_since ? UI.relTime(ch.connected_since) : '—';
      const attempts = ch.restart_attempts != null ? String(ch.restart_attempts) : '0';

      let configJson = '';
      try {
        configJson = JSON.stringify(ch, null, 2);
      } catch {
        configJson = String(ch);
      }

      return `<article class="ch-card" style="--i:${i}">
        <header class="ch-card__head">
          <span class="dot ${dotCls}"></span>
          <span class="ch-card__name" title="${_esc(name)}">${_esc(name)}</span>
          <span class="chip mono">${_esc(ch.type || 'unknown')}</span>
        </header>
        <div class="ch-card__status">
          <span class="chip ${chipCls}">${_esc(status)}</span>
        </div>
        <dl class="ch-card__meta">
          <div><dt>Connected</dt><dd class="ch-mono">${_esc(since)}</dd></div>
          <div><dt>Restart attempts</dt><dd class="ch-mono">${_esc(attempts)}</dd></div>
        </dl>
        ${_renderAccessPanel(ch)}
        <details class="ch-card__config">
          <summary>Adapter config</summary>
          <pre class="ch-card__config-pre">${_esc(configJson)}</pre>
        </details>
        <footer class="ch-card__footnote">
          <span>${_esc(_statusHint({ status, isRunning, isDead, enabled: ch.enabled !== false, name }))}</span>
        </footer>
      </article>`;
    }).join('');

    _bindAccessActions(container);
  }

  function _renderAccessPanel(ch) {
    const access = ch.access;
    if (!access || ch.type !== 'telegram') return '';
    const pending = Array.isArray(access.pending) ? access.pending : [];
    const approved = Array.isArray(access.approved) ? access.approved : [];
    const validModes = new Set(['pairing', 'allowlist', 'open', 'disabled']);
    const mode = validModes.has(access.mode) ? access.mode : 'pairing';
    const locked = Number(access.locked_until || 0) * 1000 > Date.now();
    return `<section class="ch-access${pending.length ? ' ch-access--pending' : ''}">
      <div class="ch-access__head">
        <div>
          <span class="ch-access__eyebrow">Telegram accounts</span>
          <h4 class="ch-access__title">Chat access</h4>
        </div>
        <label class="ch-access__mode">
          <span>Mode</span>
          <select data-access-mode data-channel="${_esc(ch.name || '')}" aria-label="Telegram chat access mode">
            <option value="pairing"${mode === 'pairing' ? ' selected' : ''}>Pairing codes</option>
            <option value="allowlist"${mode === 'allowlist' ? ' selected' : ''}>Allowlist only</option>
            <option value="open"${mode === 'open' ? ' selected' : ''}>Open to everyone</option>
            <option value="disabled"${mode === 'disabled' ? ' selected' : ''}>Disabled</option>
          </select>
        </label>
      </div>
      ${locked ? '<p class="ch-access__warning">Pairing approval is locked for one hour after repeated invalid codes.</p>' : ''}
      ${mode === 'open' ? `
        <p class="ch-access__note">Every Telegram account can DM this bot. Group access remains separately controlled as ${_esc(access.group_mode || 'allowlist')}.</p>
      ` : mode === 'disabled' ? `
        <p class="ch-access__note">Telegram direct messages are disabled. Group access remains separately controlled as ${_esc(access.group_mode || 'allowlist')}.</p>
      ` : `
        <div class="ch-access__group">
          <div class="ch-access__group-title">Pending <span>${pending.length}</span></div>
          ${pending.length ? `<div class="ch-access__people">
            ${pending.map(item => _renderPendingAccount(ch.name, item)).join('')}
          </div>` : '<p class="ch-access__empty">No Telegram accounts are waiting for approval.</p>'}
        </div>
        <div class="ch-access__group">
          <div class="ch-access__group-title">Approved <span>${approved.length}</span></div>
          ${approved.length ? `<div class="ch-access__people">
            ${approved.map(item => _renderApprovedAccount(ch.name, item)).join('')}
          </div>` : '<p class="ch-access__empty">No approved accounts yet.</p>'}
        </div>
      `}
    </section>`;
  }

  function _renderPendingAccount(channelName, item) {
    const senderId = String(item.sender_id || '');
    return `<div class="ch-access__person">
      <div class="ch-access__identity">
        <strong>${_esc(_senderLabel(item))}</strong>
        <span>${_esc(_senderMeta(item))}</span>
        <code class="ch-access__code">${_esc(item.code || '')}</code>
      </div>
      <div class="ch-access__person-actions">
        <button class="btn btn--primary" type="button" data-access-decision="approve" data-channel="${_esc(channelName || '')}" data-sender-id="${_esc(senderId)}">Approve</button>
        <button class="btn btn--danger" type="button" data-access-decision="deny" data-channel="${_esc(channelName || '')}" data-sender-id="${_esc(senderId)}">Deny</button>
      </div>
    </div>`;
  }

  function _renderApprovedAccount(channelName, item) {
    const senderId = String(item.sender_id || '');
    return `<div class="ch-access__person">
      <div class="ch-access__identity">
        <strong>${_esc(_senderLabel(item))}</strong>
        <span>${_esc(_senderMeta(item))}</span>
      </div>
      <button class="btn btn--ghost" type="button" data-access-revoke data-channel="${_esc(channelName || '')}" data-sender-id="${_esc(senderId)}">Revoke</button>
    </div>`;
  }

  function _bindAccessActions(container) {
    container.querySelectorAll('[data-access-mode]').forEach(select => {
      select.addEventListener('change', async () => {
        select.disabled = true;
        try {
          await _rpc.call('channels.access.setMode', {
            channel: select.dataset.channel,
            mode: select.value,
          });
          UI.toast('Telegram DM policy: ' + select.value + '.', select.value === 'open' ? 'warn' : 'info');
          await _loadData();
        } catch (err) {
          UI.toast('Failed to update access mode: ' + err.message, 'err');
          await _loadData();
        }
      });
    });

    container.querySelectorAll('[data-access-decision]').forEach(button => {
      button.addEventListener('click', async () => {
        const approved = button.dataset.accessDecision === 'approve';
        button.disabled = true;
        try {
          await _rpc.call('channels.access.resolve', {
            channel: button.dataset.channel,
            senderId: button.dataset.senderId,
            approved,
          });
          UI.toast(approved ? 'Telegram account approved.' : 'Telegram account denied.', approved ? 'info' : 'warn');
          await _loadData();
        } catch (err) {
          UI.toast('Failed to resolve account request: ' + err.message, 'err');
          button.disabled = false;
        }
      });
    });

    container.querySelectorAll('[data-access-revoke]').forEach(button => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          await _rpc.call('channels.access.revoke', {
            channel: button.dataset.channel,
            senderId: button.dataset.senderId,
          });
          UI.toast('Telegram account access revoked.', 'info');
          await _loadData();
        } catch (err) {
          UI.toast('Failed to revoke account: ' + err.message, 'err');
          button.disabled = false;
        }
      });
    });
  }

  function _senderLabel(item) {
    if (item.username) return '@' + item.username;
    if (item.display_name) return item.display_name;
    return 'Telegram user ' + (item.sender_id || 'unknown');
  }

  function _senderMeta(item) {
    const bits = [];
    if (item.display_name && item.display_name !== _senderLabel(item)) bits.push(item.display_name);
    if (item.sender_id) bits.push('ID ' + item.sender_id);
    if (item.expires_at) bits.push('expires ' + new Date(Number(item.expires_at) * 1000).toLocaleTimeString());
    if (item.source) bits.push(item.source);
    return bits.join(' · ');
  }

  function _statusHint({ status, isRunning, isDead, enabled, name }) {
    const safeName = name || '<name>';
    if (!enabled) return `Disabled in config — gateway restart required after re-enabling. Run \`agentos onboard configure channels\` to change.`;
    if (isDead) return `Adapter is dead. Inspect gateway logs, then \`agentos channels restart ${safeName}\`.`;
    if (isRunning) return 'Adapter is live in the current gateway process.';
    if (status === 'restarting') return 'Adapter is restarting after dispatch errors.';
    if (status === 'exhausted') return `Adapter exhausted its retry budget. Try \`agentos channels restart ${safeName}\`.`;
    return 'Configured on disk but not active in this gateway process — restart the gateway to load it.';
  }

  function _needsAttention(status) {
    return status === 'dead' || status === 'restarting' || status === 'exhausted';
  }

  function _inactiveHint(inactive, disabled) {
    if (!inactive) return 'no inactive channels';
    if (disabled) return `${disabled} disabled`;
    return 'configured but idle';
  }

  function _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  return { render, destroy };
})();

window.ChannelsView = ChannelsView;
