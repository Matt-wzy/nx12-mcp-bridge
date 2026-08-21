"""Run this file as a Python journal inside Siemens NX to stop the bridge."""

from nx_mcp.nx_bridge import stop_bridge


if __name__ == "__main__":
    stop_bridge()
    print("NX MCP bridge stopped")
