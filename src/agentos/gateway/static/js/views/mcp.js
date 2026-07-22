/** AgentOS Web UI - MCP server configuration and authorization. */

const MCPView = (() => {
  const ROBINHOOD_URL = 'https://agent.robinhood.com/mcp/trading';
  const ROBINHOOD_HELP = 'https://robinhood.com/us/en/support/articles/agentic-trading-overview/#ConnectyourAIagent';
  let _el = null;
  let _rpc = null;
  let _config = null;
  let _status = { enabled: false, servers: [] };
  let _editing = null;
  let _busy = false;
  let _editorKeydown = null;
  let _editorReturnSelector = null;

  const _esc = (value) => String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char]);

  function render(el) {
    _el = el;
    _rpc = App.getRpc();
    _renderLoading();
    _load();
  }

  function destroy() {
    _teardownEditorA11y();
    _el = null;
    _rpc = null;
    _config = null;
    _editing = null;
    _busy = false;
    _editorReturnSelector = null;
  }

  function _renderLoading() {
    if (!_el) return;
    _el.innerHTML = `
      <section class="mcp-stage" aria-busy="true">
        <div class="mcp-skeleton mcp-skeleton--title"></div>
        <div class="mcp-skeleton mcp-skeleton--hero"></div>
        <div class="mcp-skeleton mcp-skeleton--row"></div>
      </section>`;
  }

  async function _load() {
    const rpc = _rpc;
    try {
      await rpc.waitForConnection();
      const [config, status] = await Promise.all([
        rpc.call('config.get'),
        rpc.call('mcp.status'),
      ]);
      if (!_el || _rpc !== rpc) return;
      _config = config || {};
      _status = status || { enabled: false, servers: [] };
      if (Router.currentPath() === '/mcp/oauth/callback') {
        await _completeOAuthCallback();
        return;
      }
      _render();
    } catch (error) {
      if (!_el || _rpc !== rpc) return;
      _renderError(error);
    }
  }

  function _renderError(error) {
    if (!_el) return;
    _el.innerHTML = `
      <section class="mcp-stage">
        <div class="mcp-state mcp-state--error" role="alert">
          <h1>MCP configuration unavailable</h1>
          <p>${_esc(error?.message || error || 'Unable to load MCP settings.')}</p>
          <button class="btn" data-mcp-retry>Retry</button>
        </div>
      </section>`;
    _el.querySelector('[data-mcp-retry]')?.addEventListener('click', _load);
  }

  function _render() {
    if (!_el || !_config) return;
    _teardownEditorA11y();
    const mcp = _config.mcp || { enabled: false, servers: [] };
    const servers = Array.isArray(mcp.servers) ? mcp.servers : [];
    const statusByName = Object.fromEntries((_status.servers || []).map(item => [item.name, item]));
    const enabled = Boolean(mcp.enabled);
    const robinhood = _robinhoodSummary(servers, statusByName, enabled);

    _el.innerHTML = `
      <section class="mcp-stage">
        <header class="mcp-header">
          <div>
            <div class="mcp-eyebrow">Connections</div>
            <h1>MCP Servers</h1>
            <p>A secure control plane for the tools your agent can discover and use.</p>
          </div>
          <label class="mcp-master-toggle">
            <span>
              <strong>MCP runtime</strong>
              <small>${enabled ? 'New connections are enabled' : 'All MCP connections are paused'}</small>
            </span>
            <input type="checkbox" data-mcp-enabled ${enabled ? 'checked' : ''} aria-label="Enable MCP runtime">
            <span class="mcp-switch" aria-hidden="true"></span>
          </label>
        </header>

        <article class="mcp-partner mcp-partner--${robinhood.tone}">
          <div class="mcp-partner__content">
            <div class="mcp-partner__topline">
              <div class="mcp-partner__brand">
                <img src="${_esc(_basePath())}/static/img/robinhood-symbol.png" alt="Robinhood" width="56" height="56">
                <div>
                  <span class="mcp-partner__label">Featured integration</span>
                  <h2>Robinhood <span>× AgentOS</span></h2>
                </div>
              </div>
              <span class="mcp-partner__state mcp-partner__state--${robinhood.tone}">
                <span aria-hidden="true"></span>${_esc(robinhood.label)}
              </span>
            </div>
            <h3>Give your agent a controlled path to the market.</h3>
            <p>Connect a dedicated Agentic Trading account with secure authorization and live MCP tool discovery.</p>
            <div class="mcp-partner__capabilities" aria-label="Connection capabilities">
              <span>${icons.check()} OAuth + PKCE</span>
              <span>${icons.check()} Streamable HTTP</span>
              <span>${icons.check()} Live tool registration</span>
            </div>
            <div class="mcp-partner__actions">
              <button class="btn btn--primary mcp-partner__cta" data-mcp-robinhood>${robinhood.cta}</button>
              <a href="${ROBINHOOD_HELP}" target="_blank" rel="noopener noreferrer">Read setup guide</a>
            </div>
          </div>

          <div class="mcp-connection" aria-label="Robinhood MCP connection details">
            <div class="mcp-connection__header">
              <span>Connection architecture</span>
              <strong>${_esc(robinhood.detail)}</strong>
            </div>
            <div class="mcp-connection__flow" aria-label="AgentOS connects securely to Robinhood MCP">
              <div class="mcp-connection__node">
                <span class="mcp-connection__mark">${icons.mcp()}</span>
                <span><small>Local gateway</small><strong>AgentOS</strong></span>
              </div>
              <div class="mcp-connection__rail" aria-hidden="true"><span>OAuth</span></div>
              <div class="mcp-connection__node">
                <img src="${_esc(_basePath())}/static/img/robinhood-symbol.png" alt="" width="36" height="36">
                <span><small>Remote server</small><strong>Robinhood MCP</strong></span>
              </div>
            </div>
            <dl class="mcp-connection__specs">
              <div><dt>Endpoint</dt><dd><code title="${ROBINHOOD_URL}">${ROBINHOOD_URL}</code></dd></div>
              <div><dt>Transport</dt><dd>Streamable HTTP</dd></div>
              <div><dt>Authorization</dt><dd>OAuth with PKCE</dd></div>
              <div><dt>Tool loading</dt><dd>${_esc(robinhood.tools)}</dd></div>
            </dl>
          </div>

          <div class="mcp-partner__notice" role="note">
            ${icons.info()}
            <span><strong>Human-controlled by design.</strong> You approve the account link and remain responsible for every order. Agentic trading involves significant risk.</span>
          </div>
        </article>

        <div class="mcp-risk" role="note">
          ${icons.info()}
          <div><strong>Review every MCP permission.</strong> Connected servers can expose private data and tools that take actions on your behalf.</div>
        </div>

        <div class="mcp-section-head">
          <div>
            <h2>Your servers</h2>
            <p>${servers.length ? `${servers.length} configured connection${servers.length === 1 ? '' : 's'}` : 'No custom servers configured yet'}</p>
          </div>
          <button class="btn" data-mcp-add>${icons.plus()} Add server</button>
        </div>

        <div class="mcp-list" data-mcp-list>
          ${servers.length ? servers.map(server => _serverRow(server, statusByName[server.name], enabled)).join('') : _emptyState()}
        </div>

        ${_editing ? _editor(_editing) : ''}
      </section>`;

    _bind();
  }

  function _basePath() {
    return document.getElementById('agentos-data')?.dataset.basePath || '/control';
  }

  function _hasRobinhood(servers) {
    return servers.some(server => server.url === ROBINHOOD_URL);
  }

  function _robinhoodSummary(servers, statusByName, enabled) {
    const server = servers.find(item => item.url === ROBINHOOD_URL);
    const status = server ? statusByName[server.name] : null;
    const toolCount = Array.isArray(status?.tools) ? status.tools.length : 0;
    if (!server) return { tone: 'ready', label: 'Ready to connect', detail: 'Secure setup ready', tools: 'Discovered on connect', cta: 'Connect Robinhood' };
    if (!enabled) return { tone: 'paused', label: 'Runtime paused', detail: 'Configured · paused', tools: 'Available when enabled', cta: 'Review connection' };
    if (status?.connected) return { tone: 'live', label: 'Connected', detail: `${toolCount} live tool${toolCount === 1 ? '' : 's'}`, tools: `${toolCount} registered`, cta: 'Manage connection' };
    if (server.oauth && !status?.authenticated) return { tone: 'auth', label: 'OAuth required', detail: 'Ready for authorization', tools: 'Loads after authorization', cta: 'Authorize Robinhood' };
    return { tone: 'ready', label: 'Ready to connect', detail: 'Configuration saved', tools: 'Discovered on connect', cta: 'Review connection' };
  }

  function _serverRow(server, status, enabled) {
    let state = 'Disconnected';
    let stateClass = 'off';
    if (!enabled) state = 'Paused';
    else if (status?.connected) { state = 'Connected'; stateClass = 'ok'; }
    else if (server.oauth && !status?.authenticated) { state = 'Authorization required'; stateClass = 'warn'; }
    const detail = server.transport === 'stdio'
      ? [server.command, ...(server.args || [])].filter(Boolean).join(' ')
      : server.url;
    const toolCount = Array.isArray(status?.tools) ? status.tools.length : 0;
    return `
      <article class="mcp-server" data-server="${_esc(server.name)}">
        <div class="mcp-server__main">
          <div class="mcp-server__title-row">
            <h3>${_esc(server.name)}</h3>
            <span class="mcp-status mcp-status--${stateClass}"><span aria-hidden="true"></span>${state}</span>
          </div>
          <div class="mcp-server__meta">
            <span>${_transportLabel(server.transport)}</span>
            ${server.oauth ? '<span>OAuth</span>' : ''}
            ${toolCount ? `<span>${toolCount} tool${toolCount === 1 ? '' : 's'}</span>` : ''}
          </div>
          <code title="${_esc(detail || '')}">${_esc(detail || 'Configuration incomplete')}</code>
        </div>
        <div class="mcp-server__actions">
          ${enabled && !status?.connected ? `<button class="btn btn--primary" data-mcp-connect="${_esc(server.name)}">${server.oauth && !status?.authenticated ? 'Authorize' : 'Connect'}</button>` : ''}
          ${status?.connected ? `<button class="btn" data-mcp-disconnect="${_esc(server.name)}">Disconnect</button>` : ''}
          <button class="btn btn--icon" data-mcp-edit="${_esc(server.name)}" aria-label="Edit ${_esc(server.name)}" title="Edit">${icons.edit()}</button>
          <button class="btn btn--icon" data-mcp-remove="${_esc(server.name)}" aria-label="Remove ${_esc(server.name)}" title="Remove">${icons.trash()}</button>
        </div>
      </article>`;
  }

  function _transportLabel(value) {
    return ({ streamable_http: 'Streamable HTTP', sse: 'SSE', stdio: 'Local process' })[value] || value;
  }

  function _emptyState() {
    return `
      <div class="mcp-state">
        <div class="mcp-state__icon">${icons.mcp()}</div>
        <h3>No MCP servers</h3>
        <p>Add a server URL or choose the Robinhood connection above.</p>
        <button class="btn" data-mcp-add-empty>Add server</button>
      </div>`;
  }

  function _newServer(overrides = {}) {
    return {
      originalName: null,
      name: '',
      transport: 'streamable_http',
      url: '',
      command: null,
      args: [],
      env: {},
      headers: {},
      oauth: false,
      tool_timeout_seconds: 30,
      ...overrides,
    };
  }

  function _editor(server) {
    const isHttp = server.transport !== 'stdio';
    return `
      <div class="mcp-dialog-backdrop" data-mcp-dialog-backdrop>
        <section class="mcp-dialog" role="dialog" aria-modal="true" aria-labelledby="mcp-editor-title" aria-describedby="mcp-editor-description" data-mcp-dialog tabindex="-1">
          <form class="mcp-editor" data-mcp-form novalidate>
            <header class="mcp-editor__head">
              <div>
                <span class="mcp-editor__eyebrow">MCP connection</span>
                <h2 id="mcp-editor-title">${server.originalName ? 'Edit server' : 'Add MCP server'}</h2>
                <p id="mcp-editor-description">Changes apply immediately after a successful connection.</p>
              </div>
              <button type="button" class="btn btn--icon" data-mcp-cancel aria-label="Close dialog">${icons.x()}</button>
            </header>
            <div class="mcp-editor__body">
              <div class="mcp-form-grid">
                <label class="mcp-field">
                  <span>Name (required)</span>
                  <input name="name" value="${_esc(server.name)}" autocomplete="off" required maxlength="64" placeholder="my-mcp-server">
                  <small>Unique name used in logs and configuration.</small>
                  <em data-error-for="name" role="alert"></em>
                </label>
                <label class="mcp-field">
                  <span>Transport</span>
                  <select name="transport">
                    <option value="streamable_http" ${server.transport === 'streamable_http' ? 'selected' : ''}>Streamable HTTP</option>
                    <option value="sse" ${server.transport === 'sse' ? 'selected' : ''}>SSE (legacy)</option>
                    <option value="stdio" ${server.transport === 'stdio' ? 'selected' : ''}>Local process (stdio)</option>
                  </select>
                  <small>Use Streamable HTTP for new remote servers.</small>
                </label>
              </div>
              <div data-http-fields ${isHttp ? '' : 'hidden'}>
                <label class="mcp-field">
                  <span>Server URL (required)</span>
                  <input name="url" type="url" value="${_esc(server.url || '')}" autocomplete="url" placeholder="https://example.com/mcp">
                  <small>HTTPS is recommended. Robinhood uses ${ROBINHOOD_URL}</small>
                  <em data-error-for="url" role="alert"></em>
                </label>
                <label class="mcp-check" data-oauth-field ${server.transport === 'streamable_http' ? '' : 'hidden'}>
                  <input name="oauth" type="checkbox" ${server.oauth ? 'checked' : ''}>
                  <span><strong>Authenticate with OAuth</strong><small>Open the provider's authorization page and store tokens privately in AgentOS state.</small></span>
                </label>
                <details class="mcp-advanced">
                  <summary>Custom headers</summary>
                  <label class="mcp-field">
                    <span>Headers (JSON)</span>
                    <textarea name="headers" rows="4" spellcheck="false">${_esc(JSON.stringify(server.headers || {}, null, 2))}</textarea>
                    <small>Use environment-backed configuration for long-lived secrets when possible.</small>
                    <em data-error-for="headers" role="alert"></em>
                  </label>
                </details>
              </div>
              <div data-stdio-fields ${isHttp ? 'hidden' : ''}>
                <label class="mcp-field">
                  <span>Command (required)</span>
                  <input name="command" value="${_esc(server.command || '')}" autocomplete="off" placeholder="uvx">
                  <em data-error-for="command" role="alert"></em>
                </label>
                <label class="mcp-field">
                  <span>Arguments</span>
                  <input name="args" value="${_esc((server.args || []).join(' '))}" autocomplete="off" placeholder="package-name --flag">
                  <small>Arguments are split on spaces. Use the YAML config for complex quoting.</small>
                </label>
              </div>
              <label class="mcp-field mcp-field--timeout">
                <span>Tool timeout (seconds)</span>
                <input name="timeout" type="number" min="1" max="600" step="1" value="${_esc(server.tool_timeout_seconds || 30)}">
              </label>
            </div>
            <footer class="mcp-editor__foot">
              <span class="mcp-save-status" role="status" aria-live="polite"></span>
              <button type="button" class="btn" data-mcp-cancel>Cancel</button>
              <button type="submit" class="btn btn--primary">Save and connect</button>
            </footer>
          </form>
        </section>
      </div>`;
  }

  function _bind() {
    if (!_el) return;
    _el.querySelector('[data-mcp-enabled]')?.addEventListener('change', _toggleEnabled);
    _el.querySelector('[data-mcp-robinhood]')?.addEventListener('click', _openRobinhood);
    _el.querySelector('[data-mcp-add]')?.addEventListener('click', () => _openEditor(_newServer(), '[data-mcp-add]'));
    _el.querySelector('[data-mcp-add-empty]')?.addEventListener('click', () => _openEditor(_newServer(), '[data-mcp-add-empty]'));
    _el.querySelectorAll('[data-mcp-edit]').forEach(btn => btn.addEventListener('click', () => {
      const server = (_config.mcp?.servers || []).find(item => item.name === btn.dataset.mcpEdit);
      if (server) _openEditor(_newServer({ ...server, originalName: server.name }), `[data-mcp-edit="${CSS.escape(server.name)}"]`);
    }));
    _el.querySelectorAll('[data-mcp-remove]').forEach(btn => btn.addEventListener('click', () => _remove(btn.dataset.mcpRemove)));
    _el.querySelectorAll('[data-mcp-connect]').forEach(btn => btn.addEventListener('click', () => _connect(btn.dataset.mcpConnect, btn)));
    _el.querySelectorAll('[data-mcp-disconnect]').forEach(btn => btn.addEventListener('click', () => _disconnect(btn.dataset.mcpDisconnect, btn)));
    _bindEditor();
  }

  function _bindEditor() {
    const form = _el?.querySelector('[data-mcp-form]');
    const dialog = _el?.querySelector('[data-mcp-dialog]');
    const backdrop = _el?.querySelector('[data-mcp-dialog-backdrop]');
    if (!form || !dialog || !backdrop) return;
    document.body.classList.add('mcp-dialog-open');
    form.querySelectorAll('[data-mcp-cancel]').forEach(btn => btn.addEventListener('click', _closeEditor));
    backdrop.addEventListener('click', event => {
      if (event.target === backdrop) _closeEditor();
    });
    _editorKeydown = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        _closeEditor();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...dialog.querySelectorAll('button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter(item => item.offsetParent !== null);
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', _editorKeydown);
    form.transport.addEventListener('change', () => {
      const stdio = form.transport.value === 'stdio';
      form.querySelector('[data-http-fields]').hidden = stdio;
      form.querySelector('[data-stdio-fields]').hidden = !stdio;
      form.querySelector('[data-oauth-field]').hidden = form.transport.value !== 'streamable_http';
      if (form.transport.value !== 'streamable_http') form.oauth.checked = false;
    });
    form.querySelectorAll('input, textarea, select').forEach(input => input.addEventListener('blur', () => _validateField(form, input.name)));
    form.addEventListener('submit', _saveForm);
    requestAnimationFrame(() => form.elements.name?.focus());
  }

  function _teardownEditorA11y() {
    if (_editorKeydown) document.removeEventListener('keydown', _editorKeydown);
    _editorKeydown = null;
    document.body.classList.remove('mcp-dialog-open');
  }

  function _closeEditor() {
    if (_busy) return;
    const returnSelector = _editorReturnSelector;
    _editing = null;
    _editorReturnSelector = null;
    _render();
    requestAnimationFrame(() => {
      if (returnSelector) _el?.querySelector(returnSelector)?.focus();
    });
  }

  function _openEditor(server, returnSelector = '[data-mcp-add]') {
    _editing = server;
    _editorReturnSelector = returnSelector;
    _render();
  }

  function _openRobinhood() {
    const existing = (_config.mcp?.servers || []).find(item => item.url === ROBINHOOD_URL);
    if (existing) {
      _openEditor(_newServer({ ...existing, originalName: existing.name }), '[data-mcp-robinhood]');
      return;
    }
    _openEditor(_newServer({ name: 'robinhood-trading', url: ROBINHOOD_URL, oauth: true }), '[data-mcp-robinhood]');
  }

  function _validateField(form, name) {
    const error = form.querySelector(`[data-error-for="${name}"]`);
    if (!error) return true;
    let message = '';
    if (name === 'name') {
      const value = form.name.value.trim();
      if (!value) message = 'Enter a server name.';
      else if (!/^[a-zA-Z0-9._-]+$/.test(value)) message = 'Use letters, numbers, dots, underscores, or hyphens.';
      else if ((_config.mcp?.servers || []).some(item => item.name === value && item.name !== _editing.originalName)) message = 'This server name already exists.';
    }
    if (name === 'url' && form.transport.value !== 'stdio') {
      try {
        const url = new URL(form.url.value.trim());
        if (!['http:', 'https:'].includes(url.protocol)) message = 'Use an HTTP or HTTPS URL.';
      } catch { message = 'Enter a valid absolute URL.'; }
    }
    if (name === 'command' && form.transport.value === 'stdio' && !form.command.value.trim()) message = 'Enter a command.';
    if (name === 'headers') {
      try {
        const value = JSON.parse(form.headers.value || '{}');
        if (!value || Array.isArray(value) || typeof value !== 'object') message = 'Headers must be a JSON object.';
      } catch { message = 'Enter valid JSON.'; }
    }
    error.textContent = message;
    const input = form.elements[name];
    input?.setAttribute('aria-invalid', message ? 'true' : 'false');
    return !message;
  }

  async function _saveForm(event) {
    event.preventDefault();
    if (_busy) return;
    const form = event.currentTarget;
    const names = ['name', 'url', 'command', 'headers'];
    if (!names.map(name => _validateField(form, name)).every(Boolean)) {
      form.querySelector('[aria-invalid="true"]')?.focus();
      return;
    }
    const transport = form.transport.value;
    const server = {
      name: form.name.value.trim(),
      transport,
      command: transport === 'stdio' ? form.command.value.trim() : null,
      args: transport === 'stdio' ? form.args.value.trim().split(/\s+/).filter(Boolean) : [],
      url: transport === 'stdio' ? null : form.url.value.trim(),
      env: _editing.env || {},
      headers: transport === 'stdio' ? {} : JSON.parse(form.headers.value || '{}'),
      oauth: transport === 'streamable_http' && form.oauth.checked,
      tool_timeout_seconds: Number(form.timeout.value) || 30,
    };
    const current = [...(_config.mcp?.servers || [])];
    const index = current.findIndex(item => item.name === _editing.originalName);
    if (index >= 0) current[index] = server;
    else current.push(server);
    _setBusy(form, true, 'Saving connection...');
    try {
      await _saveServers(current, true);
      _editing = null;
      _editorReturnSelector = null;
      await _refresh();
      const button = _el?.querySelector(`[data-mcp-connect="${CSS.escape(server.name)}"]`);
      await _connect(server.name, button);
    } catch (error) {
      UI.toast(error?.message || String(error), 'error', 6000);
      _setBusy(form, false, 'Save failed. Review the fields and retry.');
    }
  }

  function _setBusy(form, busy, message = '') {
    _busy = busy;
    form?.querySelectorAll('button, input, textarea, select').forEach(item => { item.disabled = busy; });
    const status = form?.querySelector('.mcp-save-status');
    if (status) status.textContent = message;
  }

  async function _saveServers(servers, enabled = Boolean(_config.mcp?.enabled)) {
    await _rpc.call('config.patch', { patches: { 'mcp.enabled': enabled, 'mcp.servers': servers } });
    _config.mcp = { ...(_config.mcp || {}), enabled, servers };
  }

  async function _toggleEnabled(event) {
    const enabled = event.currentTarget.checked;
    event.currentTarget.disabled = true;
    try {
      await _saveServers(_config.mcp?.servers || [], enabled);
      if (!enabled) {
        await Promise.all((_config.mcp?.servers || []).map(server => _rpc.call('mcp.disconnect', { name: server.name }).catch(() => null)));
      }
      await _refresh();
      UI.toast(enabled ? 'MCP runtime enabled.' : 'MCP runtime paused.', 'success');
    } catch (error) {
      event.currentTarget.checked = !enabled;
      event.currentTarget.disabled = false;
      UI.toast(error?.message || String(error), 'error', 6000);
    }
  }

  async function _connect(name, button) {
    if (_busy || !_config.mcp?.enabled) return;
    _busy = true;
    if (button) { button.disabled = true; button.textContent = 'Connecting...'; }
    try {
      const result = await _rpc.call('mcp.connect', { name });
      if (result.authorizationRequired) {
        await _authorize(name, button);
        return;
      }
      UI.toast(`${name} connected.`, 'success');
      await _refresh();
    } catch (error) {
      UI.toast(error?.message || String(error), 'error', 7000);
      await _refresh();
    } finally {
      _busy = false;
    }
  }

  async function _authorize(name, button) {
    if (button) button.textContent = 'Opening sign in...';
    const redirectUri = `${location.origin}${_basePath()}/mcp/oauth/callback`;
    const result = await _rpc.call('mcp.oauth.start', { name, redirectUri });
    if (result.connected) {
      UI.toast(`${name} connected.`, 'success');
      await _refresh();
      return;
    }
    if (!result.authorizationUrl) throw new Error('The MCP server did not provide an authorization URL.');
    location.assign(result.authorizationUrl);
  }

  async function _completeOAuthCallback() {
    if (!_el) return;
    const params = new URLSearchParams(location.search);
    const code = params.get('code');
    const state = params.get('state');
    const error = params.get('error');
    const invalid = Boolean(error || !code || !state);
    _el.innerHTML = `
      <section class="mcp-stage">
        <div class="mcp-state" aria-live="polite">
          <div class="mcp-state__icon">${invalid ? icons.x() : icons.refresh()}</div>
          <h1>${invalid ? 'Authorization not completed' : 'Completing authorization'}</h1>
          <p>${invalid ? _esc(params.get('error_description') || error || 'The callback is missing its authorization code or state.') : 'Exchanging the authorization code and loading MCP tools.'}</p>
          ${invalid ? '<button class="btn" data-mcp-back>Back to MCP servers</button>' : ''}
        </div>
      </section>`;
    if (invalid) {
      _el.querySelector('[data-mcp-back]')?.addEventListener('click', () => Router.navigate('/mcp'));
      return;
    }
    try {
      await _rpc.call('mcp.oauth.complete', { code, state });
      history.replaceState(null, '', `${_basePath()}/mcp`);
      _status = await _rpc.call('mcp.status');
      _render();
      UI.toast('MCP authorization complete.', 'success');
    } catch (authError) {
      _renderError(authError);
    }
  }

  async function _disconnect(name, button) {
    if (button) button.disabled = true;
    try {
      await _rpc.call('mcp.disconnect', { name });
      UI.toast(`${name} disconnected.`, 'success');
      await _refresh();
    } catch (error) {
      UI.toast(error?.message || String(error), 'error', 6000);
      if (button) button.disabled = false;
    }
  }

  async function _remove(name) {
    const confirmed = await UI.confirm({
      title: 'Remove MCP server',
      message: `<p>Remove <strong>${_esc(name)}</strong> from AgentOS? Stored OAuth tokens are also cleared.</p>`,
      confirmLabel: 'Remove',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await _rpc.call('mcp.oauth.clear', { name }).catch(() => _rpc.call('mcp.disconnect', { name }));
      const servers = (_config.mcp?.servers || []).filter(item => item.name !== name);
      await _saveServers(servers);
      await _refresh();
      UI.toast(`${name} removed.`, 'success');
    } catch (error) {
      UI.toast(error?.message || String(error), 'error', 6000);
    }
  }

  async function _refresh() {
    const [config, status] = await Promise.all([_rpc.call('config.get'), _rpc.call('mcp.status')]);
    if (!_el) return;
    _config = config || {};
    _status = status || { enabled: false, servers: [] };
    _busy = false;
    _render();
  }

  return { render, destroy };
})();

window.MCPView = MCPView;
