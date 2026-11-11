"""Windows desktop app-tools adapter; no daemon and no additional model process.

Protocol follows the installed codex-app-tools server.mjs (0.1.4): JSON-RPC
frames prefixed by a little-endian uint32 length over CODEX_APP_TOOLS_PIPE_PATH.
This is a version-sensitive local interface, so validate the live tool catalog.
Run pipe I/O in a short-lived subprocess to bound blocking Windows pipe reads.
"""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import uuid

MAX_FRAME = 8 * 1024 * 1024


class DesktopError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def request(payload, timeout=30):
    try:
        response = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve())],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise DesktopError("Desktop pipe timed out; delivery may have been accepted.",
                           payload["mode"] == "send") from exc
    except OSError as exc:
        raise DesktopError(str(exc)) from exc
    try:
        value = json.loads(response.stdout)
        if not isinstance(value, dict) or "ok" not in value:
            raise ValueError("Invalid bridge response")
    except ValueError as exc:
        raise DesktopError("Invalid desktop bridge response: " + response.stderr[-1000:],
                           payload["mode"] == "send") from exc
    if not value["ok"]:
        raise DesktopError(value["error"], value.get("uncertain", False))
    return value["result"]


def probe(thread_id=None):
    pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "")
    caller = os.environ.get("CODEX_THREAD_ID", "")
    target = thread_id or caller
    try:
        caller, target = str(uuid.UUID(caller)), str(uuid.UUID(target))
    except ValueError as exc:
        raise DesktopError("Run inside the receiving Codex desktop task; CODEX_THREAD_ID is required.") from exc
    if not pipe.startswith("\\\\.\\pipe\\"):
        raise DesktopError("Codex desktop did not provide a local CODEX_APP_TOOLS_PIPE_PATH.")
    result = request(dict(mode="probe", pipe=pipe, caller_thread_id=caller, thread_id=target))
    return dict(transport="desktop_app_tools", pipe=pipe, caller_thread_id=caller,
                thread_id=target, host_id=result["hostId"], probe="passed", wake_verified=False)


def send(receipt, message):
    # A retry from the same desktop task may use its newly issued pipe after restart.
    pipe = receipt["pipe"]
    if os.environ.get("CODEX_THREAD_ID") == receipt["caller_thread_id"]:
        pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH") or pipe
    return request(dict(mode="send", pipe=pipe, caller_thread_id=receipt["caller_thread_id"],
                        thread_id=receipt["thread_id"], message=message))


class PipeClient:
    def __init__(self, stream):
        self.stream = stream
        self.next_id = 0
        self.send_attempted = False

    def read_exact(self, count):
        result = bytearray()
        while len(result) < count:
            chunk = self.stream.read(count - len(result))
            if not chunk:
                raise EOFError("Desktop pipe closed before acknowledgement")
            result.extend(chunk)
        return bytes(result)

    def rpc(self, method, params, mutation=False):
        self.next_id += 1
        data = json.dumps(dict(jsonrpc="2.0", id=self.next_id, method=method, params=params),
                          ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_FRAME:
            raise ValueError("Desktop request exceeds frame limit")
        frame = memoryview(struct.pack("<I", len(data)) + data)
        if mutation:
            self.send_attempted = True
        while frame:
            written = self.stream.write(frame)
            if not written:
                raise EOFError("Desktop pipe closed during request")
            frame = frame[written:]
        size = struct.unpack("<I", self.read_exact(4))[0]
        if size > MAX_FRAME:
            raise ValueError("Desktop response exceeds frame limit")
        reply = json.loads(self.read_exact(size))
        if reply.get("jsonrpc") != "2.0" or reply.get("id") != self.next_id:
            raise ValueError("Desktop response ID mismatch")
        if "error" in reply:
            raise DesktopError(str(reply["error"]))
        return reply["result"]

    def tool(self, name, arguments, caller):
        params = dict(
            namespace="codex_app", tool=name, arguments=arguments, threadId=caller,
            callerSource="codex", callId="fpga-call-" + uuid.uuid4().hex,
            turnId="fpga-event-" + uuid.uuid4().hex)
        # Match the installed MCP adapter: an absent host is resolved by the
        # desktop. Forcing 'local' misidentifies delegated/durable callers.
        caller_host = os.environ.get("CODEX_APP_TOOLS_CALLER_HOST_ID")
        if caller_host:
            params["hostId"] = caller_host
        result = self.rpc("tools/call", params, mutation=name == "send_message_to_thread")
        texts = [i["text"] for i in result.get("contentItems", []) if i.get("type") == "inputText"]
        if result.get("success") is not True:
            raise DesktopError("Desktop rejected request: " + "\n".join(texts))
        if len(texts) != 1:
            raise ValueError("Unexpected desktop response content")
        return json.loads(texts[0])


def bridge(payload):
    client = None
    stage = "validate_context"
    try:
        if payload["mode"] not in ("probe", "send"):
            raise ValueError("Unknown desktop bridge mode")
        caller = str(uuid.UUID(payload["caller_thread_id"]))
        target = str(uuid.UUID(payload["thread_id"]))
        if not payload["pipe"].startswith("\\\\.\\pipe\\"):
            raise ValueError("Only a local Windows named pipe is supported")
        stage = "connect_pipe"
        with open(payload["pipe"], "r+b", buffering=0) as stream:
            client = PipeClient(stream)
            stage = "tools/list"
            catalog = client.rpc("tools/list", {"threadStartKind": "all"})
            tools = {t["name"]: t for t in catalog["tools"] if t.get("namespace") == "codex_app"}
            for name, required in (("read_thread", {"threadId"}),
                                   ("send_message_to_thread", {"threadId", "prompt"})):
                if name not in tools or not required <= set(tools[name]["inputSchema"].get("properties", {})):
                    raise ValueError("Desktop app-tools interface changed or is disabled: " + name)
            stage = "read_thread"
            current = client.tool("read_thread", dict(threadId=target, turnLimit=1,
                                                      includeOutputs=False, maxOutputCharsPerItem=0), caller)
            thread = current["thread"]
            if thread["id"] != target or thread.get("kind") != "codex" or thread.get("hostId") != "local":
                raise ValueError("Notification requires the requested local Codex desktop task; "
                                 "resolved target host=" + str(thread.get("hostId")) +
                                 ". Use a local desktop task or explicitly authorize foreground "
                                 "Vivado mode. Do not replace caller/target identity.")
            if payload["mode"] == "probe":
                result = {k: thread[k] for k in ("id", "hostId", "status")}
            else:
                stage = "send_message_to_thread"
                result = client.tool("send_message_to_thread", dict(threadId=target, hostId="local",
                                                                     prompt=payload["message"]), caller)
                if result.get("threadId") != target:
                    raise ValueError("Desktop acknowledged a different task")
            return dict(ok=True, result=result)
    except DesktopError as exc:
        return dict(ok=False, error=stage + ": " + str(exc),
                    uncertain=exc.uncertain or bool(client and client.send_attempted))
    except Exception as exc:
        return dict(ok=False, error=stage + ": " + str(exc), uncertain=bool(client and client.send_attempted))


if __name__ == "__main__":
    print(json.dumps(bridge(json.load(sys.stdin)), ensure_ascii=False))
