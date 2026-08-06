# MCP Tools (External Servers)

Call tools exposed by connected MCP servers with the `mcp_call` tag, wrapped
in an action block like any other skill tag.

## Tag Format

<action><mcp_call server="SERVER_NAME" tool="TOOL_NAME">{json_args}</mcp_call></action>

- `server` - name of a connected MCP server. Omit to auto-route to whichever
  connected server provides the tool.
- `tool` - the tool name to invoke (required).
- Body - a single JSON object of arguments matching the tool input schema.
- `timeout` (optional attr) - seconds to wait, default 90, max 300.

## Rules

- Only call tools that appear in the **MCP Tools** section of the system
  prompt. That section lists connected servers and their discovered tools; if
  it is absent, no MCP servers are connected - do not emit `mcp_call`.
- Pass arguments exactly as JSON. `*` marks required params in the tool list.
- The result is returned as tool output; large results are truncated for
  context, so request focused queries when possible.

## Example

<action><mcp_call server="github" tool="search_repositories">{"query": "user:sifat", "per_page": 10}</mcp_call></action>
