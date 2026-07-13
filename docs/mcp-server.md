# MCP Server Bridge

AgentOS can run as a stdio MCP server bridge for MCP-capable clients. Use
this when another local AI client should call into AgentOS session
workflows through the Model Context Protocol.

The MCP bridge is an integration surface. It is separate from AgentOS's Web
UI, CLI, channels, and gateway control console.

## Requirements

Install AgentOS with the `mcp` extra when you need this bridge. Follow the
[Installation](../README.md#installation) section of the README, and add `mcp`
to the extras list — use `agentos[recommended,mcp]` in place of
`agentos[recommended]`.

Start the AgentOS gateway:

```sh
agentos gateway run
```

Or use the managed gateway:

```sh
agentos gateway start --json
agentos gateway status
```

## Run the Bridge

```sh
agentos mcp-server run
```

By default, the bridge connects to:

```text
ws://localhost:18791/ws
```

Use a different gateway:

```sh
agentos mcp-server run --gateway ws://localhost:18792/ws
```

The command runs a stdio MCP server. Configure your MCP-capable client to launch
that command as the server process.

## Safety Notes

- Keep the gateway bound to `127.0.0.1` unless you intentionally expose it.
- Do not put provider keys or channel secrets in MCP client config examples.
- Treat the MCP client as another tool-calling surface. The same AgentOS
  permissions, tools, sessions, and gateway state still matter.

## Troubleshooting

If the bridge cannot start:

```sh
agentos gateway status
agentos doctor
```

If the command reports that MCP dependencies are missing, reinstall with the
`mcp` extra.

Read next:

- [`configuration.md`](configuration.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`operations.md`](operations.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
