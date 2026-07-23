from pathlib import Path

ROOT = Path("src/agentos/gateway")
VIEW = ROOT / "static/js/views/mcp.js"
STYLE = ROOT / "static/css/views/mcp.css"
APP = ROOT / "static/js/app.js"
TEMPLATE = ROOT / "templates/index.html"
TRUST_VISUAL = ROOT / "static/img/mcp-trust-network.webp"


def test_mcp_view_is_routed_and_bundled() -> None:
    app = APP.read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "Router.register('/mcp'" in app
    assert "Router.register('/mcp/oauth/callback'" in app
    assert 'data-path="/mcp"' in app
    assert "static/js/views/mcp.js" in template
    assert "static/css/views/mcp.css" in template


def test_mcp_navigation_is_listed_after_channels_in_control_group() -> None:
    app = APP.read_text(encoding="utf-8")

    control_idx = app.index('nav-group-label">Control')
    channels_idx = app.index('data-path="/channels"')
    mcp_idx = app.index('data-path="/mcp"')
    skills_idx = app.index('data-path="/skills"')
    settings_idx = app.index('nav-group-label">Settings')

    assert control_idx < channels_idx < mcp_idx < skills_idx < settings_idx


def test_mcp_view_includes_robinhood_streamable_http_preset() -> None:
    view = VIEW.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert "https://agent.robinhood.com/mcp/trading" in view
    assert "robinhood-symbol.png" in view
    assert "Streamable HTTP" in view
    assert "Agentic trading involves significant risk" in view
    assert "Robinhood <span>× AgentOS</span>" in view
    assert "Connection architecture" in view
    assert "OAuth + PKCE" in view
    assert "Human-controlled by design" in view
    assert "mcp-trust-network.webp" in style
    assert TRUST_VISUAL.stat().st_size < 50_000
    assert "mcp.oauth.start" in view
    assert "mcp.oauth.complete" in view


def test_mcp_view_has_full_form_and_feedback_states() -> None:
    view = VIEW.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert 'data-error-for="url"' in view
    assert 'aria-live="polite"' in view
    assert "MCP configuration unavailable" in view
    assert "No MCP servers" in view
    assert "prefers-reduced-motion" in style
    assert "@media (max-width: 760px)" in style


def test_mcp_editor_is_an_accessible_responsive_dialog() -> None:
    view = VIEW.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    assert 'role="dialog"' in view
    assert 'aria-modal="true"' in view
    assert 'aria-labelledby="mcp-editor-title"' in view
    assert "data-mcp-dialog-backdrop" in view
    assert "event.key === 'Escape'" in view
    assert "event.key !== 'Tab'" in view
    assert "document.activeElement === last" in view
    assert "mcp-dialog-open" in view
    assert ".mcp-dialog-backdrop" in style
    assert "position: fixed" in style
    assert "max-height: 92dvh" in style
    assert "mcp-dialog-sheet-in" in style
