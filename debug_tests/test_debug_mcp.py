"""Actual MCP stdio compatibility and detached offline workflows; no hardware calls."""
import asyncio
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from vivado_debug.bootstrap import enable_vendor
enable_vendor()
from mcp import Client, StdioServerParameters
from vivado_debug.mcp_server import create_server
from vivado_debug.sessions import SessionClient
from fpga_auto.runner import process_identity

PACKAGE = Path(__file__).resolve().parents[1]


class LegacyPeer:
    def __init__(self, root):
        self.log = (root / "mcp-stderr.log").open("ab")
        self.process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(PACKAGE / "mcp_server.py"), "--root", str(root)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                        text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        self.sequence = 0

        def read():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            except Exception as exc:
                self.messages.put(exc)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        response = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "vivado-debug-offline-test", "version": "1"}})
        if "result" not in response:
            raise RuntimeError(response)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        self.send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            response = self.messages.get(timeout=25)
            if isinstance(response, Exception):
                raise response
            if response.get("id") == self.sequence:
                return response
        raise TimeoutError("MCP response missing")

    def tool(self, name, arguments=None):
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in response:
            raise RuntimeError(response)
        return response["result"]

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=3)
        self.process.stdout.close()
        self.log.close()


class MCPDebugTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vivado-mcp-protocol-")
        self.root = Path(self.temp.name)
        self.sessions = []
        self.peer = None
        self.client = SessionClient(self.root)

    def tearDown(self):
        if self.peer:
            self.peer.close()
        for sid in self.sessions:
            state = self.client.get(sid)
            if state.get("job_id"):
                self.client.cancel(state["job_id"])
                self.client.wait(state["job_id"], 15)
            if self.client.get(sid)["state"] in ("ready", "busy"):
                job = self.client.submit(sid, "close")
                self.client.wait(job["job_id"], 15)
            until = time.monotonic() + 5
            while process_identity(state.get("pid")) and time.monotonic() < until:
                time.sleep(.03)
        self.temp.cleanup()

    def structured(self, result):
        self.assertFalse(result.get("isError", False), result)
        self.assertIn("structuredContent", result)
        return result["structuredContent"]

    def test_sdk_2_stdio_discovery_and_offline_boundary(self):
        async def run():
            params = StdioServerParameters(command=sys.executable,
                args=["-B", "-X", "utf8", str(PACKAGE / "mcp_server.py"), "--root", str(self.root)])
            async with Client(params) as client:
                tools = await client.list_tools()
                self.assertGreaterEqual(len(tools.tools), 15)
                info = await client.call_tool("vivado_debug_info", {})
                self.assertFalse(info.is_error)
                self.assertFalse(info.structured_content["hardware_enabled"])
                rejected = await client.call_tool("vivado_session_open", {"backend": "vivado"})
                self.assertTrue(rejected.is_error)
        asyncio.run(run())

    def test_legacy_stdio_full_vio_ila_and_recipe_workflow(self):
        self.peer = LegacyPeer(self.root)
        listing = self.peer.request("tools/list", {})["result"]
        tools = {t["name"]: t for t in listing["tools"]}
        self.assertFalse(tools["vivado_udp_snapshot"]["annotations"]["readOnlyHint"])
        opened = self.structured(self.peer.tool("vivado_session_open", {"idle_seconds": 30}))
        self.assertEqual(opened["state"], "ready")
        sid = opened["session_id"]
        self.sessions.append(sid)
        write = self.structured(self.peer.tool("vivado_vio_apply", {"session_id": sid, "endpoint": "A", "core": "traffic",
                                     "writes": {"out0": "0xfffffffffffe", "out11": "18446744073709551615"}}))
        self.assertEqual(write["execution"], "completed")
        read = self.structured(self.peer.tool("vivado_vio_read", {"session_id": sid, "endpoint": "A", "core": "traffic", "port": "out11"}))
        self.assertEqual(read["debug"]["decimal"], "18446744073709551615")
        capture = self.structured(self.peer.tool("vivado_ila_capture_start", {"session_id": sid, "endpoint": "A", "core": "status", "depth": 16}))
        self.client.wait(capture["job_id"], 15)
        data = self.structured(self.peer.tool("vivado_job_get", {"job_id": capture["job_id"]}))
        self.assertTrue(data["result"]["debug"]["capture_complete"])
        recipe = self.structured(self.peer.tool("vivado_recipe_start", {"session_id": sid, "parameters": {"packet_limit": "100", "timeout": 8}}))
        result = self.client.wait(recipe["job_id"], 15)
        self.assertEqual(result["debug"]["verdict"], "PASS")
        self.assertEqual(result["debug"]["source_kind"], "simulated")
        invalid = self.peer.tool("vivado_vio_apply", {"session_id": sid, "endpoint": "A", "core": "traffic", "writes": {"in0": "1"}})
        self.assertTrue(invalid.get("isError"), invalid)

    def test_mcp_disconnect_does_not_cancel_detached_recipe(self):
        self.peer = LegacyPeer(self.root)
        opened = self.structured(self.peer.tool("vivado_session_open", {"idle_seconds": 30}))
        sid = opened["session_id"]
        self.sessions.append(sid)
        job = self.structured(self.peer.tool("vivado_recipe_start", {"session_id": sid, "parameters": {
            "packet_limit": "0", "duration_seconds": .4, "timeout": 8}}))
        self.peer.close()
        self.peer = None
        result = self.client.wait(job["job_id"], 15)
        self.assertEqual(result["execution"], "completed", result)
        self.assertEqual(result["debug"]["verdict"], "PASS")
        next_job = self.client.submit(sid, "hw_status")
        self.assertEqual(self.client.wait(next_job["job_id"], 10)["execution"], "completed")

    def test_sdk_in_memory_honors_error_annotations(self):
        async def run():
            async with Client(create_server(self.root)) as client:
                result = await client.call_tool("vivado_ltx_describe", {"path": str(self.root / "missing.ltx")})
                self.assertTrue(result.is_error)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
