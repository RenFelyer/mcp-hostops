"""Errors addressed to the tool caller.

`UserError` subclasses fastmcp's `ToolError`: its text reaches the client as-is, without
the "Error calling tool" wrapper. Everything else (ssh, I/O, logic failures) remains a
regular exception — fastmcp wraps and logs those itself.
"""

from fastmcp.exceptions import ToolError


class UserError(ToolError):
    """The call is invalid on the client's side: wrong alias, missing password, no such job."""
