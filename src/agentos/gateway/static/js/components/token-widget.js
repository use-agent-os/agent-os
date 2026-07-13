/** AgentOS Web UI — Token usage floating widget (pill + card + drag). */

const TokenWidget = (() => {
  function _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _fmtTokens(n) {
    if (n == null) return '0';
    const v = Number(n);
    if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M';
    if (v >= 1_000) return (v / 1_000).toFixed(1) + 'K';
    return String(v);
  }

  function _fmtCost(usd) {
    if (usd == null) return '$0.0000';
    return '$' + Number(usd).toFixed(4);
  }

  const _CHEVRON_DOWN = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="2,4 6,8 10,4"/></svg>';
  const _CHEVRON_UP = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="2,8 6,4 10,8"/></svg>';

  function create(container) {
    const state = {
      input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, model: '',
      routedTurns: 0,
      sessionSaved: 0,
      mode: 'pill',
      pos: { x: 0, y: 0 },
    };
    let cacheHitWarningShown = false;

    // -- Pill --
    const pill = document.createElement('div');
    pill.className = 'token-pill';
    pill.setAttribute('role', 'status');
    pill.setAttribute('aria-label', 'Token usage');

    const dot = document.createElement('span');
    dot.className = 'token-pill__dot';
    pill.appendChild(dot);

    const pillText = document.createElement('span');
    pillText.className = 'token-pill__text';
    pill.appendChild(pillText);

    const expandBtn = document.createElement('button');
    expandBtn.className = 'token-pill__expand btn--ghost btn--icon';
    expandBtn.setAttribute('title', 'Expand');
    expandBtn.innerHTML = _CHEVRON_DOWN;
    pill.appendChild(expandBtn);

    // -- Card --
    const card = document.createElement('div');
    card.className = 'token-card token--inactive';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', 'Token usage details');

    const cardHeader = document.createElement('div');
    cardHeader.className = 'token-card__header';

    const cardTitle = document.createElement('span');
    cardTitle.className = 'token-card__title';
    cardTitle.textContent = 'Token Usage';
    cardHeader.appendChild(cardTitle);

    const closeBtn = document.createElement('button');
    closeBtn.className = 'token-card__close btn--ghost btn--icon';
    closeBtn.setAttribute('title', 'Collapse');
    closeBtn.innerHTML = _CHEVRON_UP;
    cardHeader.appendChild(closeBtn);
    card.appendChild(cardHeader);

    const cardBody = document.createElement('div');
    cardBody.className = 'token-card__body';
    card.appendChild(cardBody);

    container.appendChild(pill);
    document.body.appendChild(card);  // card is position:fixed, keep it out of flex flow

    // -- Row builder (safe DOM, no innerHTML for data) --
    function _makeRow(label, value, valueCls) {
      const row = document.createElement('div');
      row.className = 'token-card__row';
      const lbl = document.createElement('span');
      lbl.className = 'token-card__label';
      lbl.textContent = label;
      const val = document.createElement('span');
      val.className = 'token-card__value' + (valueCls ? ' ' + valueCls : '');
      val.textContent = value;
      row.appendChild(lbl);
      row.appendChild(val);
      return row;
    }

    function _makeDivider() {
      const d = document.createElement('div');
      d.className = 'token-card__divider';
      return d;
    }

    // -- Render helpers --
    function _cacheHitRatio() {
      const input = Number(state.input || 0);
      const cacheRead = Number(state.cacheRead || 0);
      if (input <= 0 || cacheRead <= 0) return null;
      if (cacheRead > input) {
        if (!cacheHitWarningShown) {
          console.warn('TokenWidget cacheRead exceeds input tokens; clamping cache hit ratio to 100%.');
          cacheHitWarningShown = true;
        }
        return 1;
      }
      return cacheRead / input;
    }

    function _renderPill() {
      const total = (state.input || 0) + (state.output || 0);
      const modelPart = state.model ? ' \u00b7 ' + state.model : '';
      const ratio = _cacheHitRatio();
      const cachePart = ratio !== null ? ' \u00b7 Cache: ' + (ratio * 100).toFixed(1) + '%' : '';
      const routedPart = state.routedTurns > 0 ? ' \u00b7 \u26a1' + state.routedTurns : '';
      const savedPart = state.sessionSaved > 0 ? ' \u00b7 \u2193' + _fmtCost(state.sessionSaved) : '';
      pillText.textContent = _fmtTokens(total) + ' tokens \u00b7 ' + _fmtCost(state.cost) + modelPart + cachePart + routedPart + savedPart;
    }

    function _renderCard() {
      const total = (state.input || 0) + (state.output || 0);
      cardBody.textContent = '';
      cardBody.appendChild(_makeRow('Input', _fmtTokens(state.input), ''));
      cardBody.appendChild(_makeRow('Output', _fmtTokens(state.output), ''));
      if (state.cacheRead > 0) {
        cardBody.appendChild(_makeRow('Cache R', _fmtTokens(state.cacheRead), 'token-card__value--meta'));
      }
      if (state.cacheWrite > 0) {
        cardBody.appendChild(_makeRow('Cache W', _fmtTokens(state.cacheWrite), 'token-card__value--meta'));
      }
      cardBody.appendChild(_makeDivider());
      const totalRow = _makeRow('Total', _fmtTokens(total), '');
      totalRow.classList.add('token-card__row--total');
      cardBody.appendChild(totalRow);
      cardBody.appendChild(_makeRow('Cost', _fmtCost(state.cost), 'token-card__value--cost'));
      const ratio = _cacheHitRatio();
      if (ratio !== null) {
        cardBody.appendChild(_makeRow('Cache hit', (ratio * 100).toFixed(1) + '%', 'token-card__value--meta'));
      }
      if (state.routedTurns > 0) {
        cardBody.appendChild(_makeRow('Routed turns', state.routedTurns + ' \u26a1', 'token-card__value--meta'));
      }
      if (state.sessionSaved > 0) {
        cardBody.appendChild(_makeRow('Session saved', _fmtCost(state.sessionSaved), 'token-card__value--saved'));
      }
      const mRow = _makeRow('Model', state.model || '\u2014', 'token-card__value--meta');
      mRow.querySelector('.token-card__value--meta').setAttribute('title', state.model || '');
      cardBody.appendChild(mRow);
    }

    function _applyMode() {
      if (state.mode === 'pill') {
        pill.classList.remove('token--inactive');
        card.classList.add('token--inactive');
        _renderPill();
      } else {
        pill.classList.add('token--inactive');
        card.classList.remove('token--inactive');
        _renderCard();
      }
    }

    function _clampToViewport(x, y, target) {
      const margin = 8;
      const width = target.offsetWidth || target.getBoundingClientRect().width;
      const height = target.offsetHeight || target.getBoundingClientRect().height;
      const maxX = Math.max(margin, window.innerWidth - width - margin);
      const maxY = Math.max(margin, window.innerHeight - height - margin);
      return {
        x: Math.min(Math.max(margin, x), maxX),
        y: Math.min(Math.max(margin, y), maxY),
      };
    }

    function _placeCardNearPill(pillRect) {
      const margin = 8;
      const cardW = card.offsetWidth || card.getBoundingClientRect().width;
      const cardH = card.offsetHeight || card.getBoundingClientRect().height;
      const toolbar = pill.closest('.chat-toolbar');
      const anchorTop = toolbar ? toolbar.getBoundingClientRect().top : pillRect.top;
      let x = pillRect.right - cardW;
      let y = anchorTop - cardH - margin;
      if (y < margin) y = pillRect.bottom + margin;
      const pos = _clampToViewport(x, y, card);
      card.style.left = pos.x + 'px';
      card.style.top = pos.y + 'px';
      card.style.right = 'auto';
      card.style.bottom = 'auto';
    }

    // -- Drag --
    function _makeDraggable(handle, target) {
      let dragging = false;
      let startX = 0, startY = 0, origX = 0, origY = 0;

      handle.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        if (e.target.closest('button')) return;  // let button clicks through
        dragging = true;
        startX = e.clientX;
        startY = e.clientY;
        const rect = target.getBoundingClientRect();
        origX = rect.left;
        origY = rect.top;
        handle.setPointerCapture(e.pointerId);
        handle.style.cursor = 'grabbing';
        e.preventDefault();
      });

      handle.addEventListener('pointermove', (e) => {
        if (!dragging) return;
        const next = _clampToViewport(
          origX + (e.clientX - startX),
          origY + (e.clientY - startY),
          target
        );
        state.pos = next;
        target.style.left = next.x + 'px';
        target.style.top = next.y + 'px';
        target.style.right = 'auto';
        target.style.bottom = 'auto';
      });

      handle.addEventListener('pointerup', () => {
        dragging = false;
        handle.style.cursor = '';
      });

      handle.addEventListener('pointercancel', () => {
        dragging = false;
        handle.style.cursor = '';
      });
    }

    _makeDraggable(pill, pill);
    _makeDraggable(cardHeader, card);

    // -- Mode toggle --
    expandBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      // Capture pill position BEFORE hiding it
      const pillRect = pill.getBoundingClientRect();
      state.mode = 'card';
      _applyMode();
      _placeCardNearPill(pillRect);
    });

    closeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      state.mode = 'pill';
      _applyMode();
    });

    _renderPill();

    return {
      update(data) {
        if (data.input != null) state.input = data.input;
        if (data.output != null) state.output = data.output;
        if (data.cacheRead != null) state.cacheRead = data.cacheRead;
        if (data.cacheWrite != null) state.cacheWrite = data.cacheWrite;
        if (data.cost != null) state.cost = data.cost;
        if (data.model != null) state.model = data.model;
        if (data.routedTurns != null) state.routedTurns = data.routedTurns;
        if (data.sessionSaved != null) state.sessionSaved = data.sessionSaved;
        if (state.mode === 'pill') _renderPill();
        else _renderCard();
      },

      reset() {
        state.input = 0;
        state.output = 0;
        state.cacheRead = 0;
        state.cacheWrite = 0;
        state.cost = 0;
        state.model = '';
        state.routedTurns = 0;
        state.sessionSaved = 0;
        if (state.mode === 'pill') _renderPill();
        else _renderCard();
      },

      destroy() {
        pill.remove();
        card.remove();
      },
    };
  }

  let _instance = null;

  return {
    create(container) {
      if (_instance) _instance.destroy();
      _instance = create(container);
    },
    update(data) { if (_instance) _instance.update(data); },
    reset() { if (_instance) _instance.reset(); },
    destroy() { if (_instance) { _instance.destroy(); _instance = null; } },
  };
})();

window.TokenWidget = TokenWidget;
