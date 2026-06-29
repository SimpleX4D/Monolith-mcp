#!/usr/bin/env python3
"""
Automated tests for Saiga HTTP API (SaigaApiHandler).

Tests endpoints:
  /api/agent/ping, /api/agent/tools, /api/agent/observe,
  /api/agent/listen, /api/agent/say, /api/agent/act,
  /api/agent/recall, /api/agent/recipes, /api/agent/where_is

Usage:
  1. Start SS14 server with saiga enabled
  2. Connect a client, join the game, run: saiga_agent on
  3. Run tests:
     python test_api.py --agent <ID> --base-url http://127.0.0.1:1212 --token devsecret

   Without --agent, only tests without agent dependency run (ping, tools, auth, errors).
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
import http.client


def post(url: str, data: dict, headers: dict | None = None, timeout: int = 10) -> tuple[int, dict]:
    """HTTP POST, returns (status_code, body_dict). Handles all HTTP/connection errors."""
    body = json.dumps(data).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        # Try to read response body even on error
        try:
            text = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(text)
            except json.JSONDecodeError:
                return e.code, {"raw": text.strip()}
        except (http.client.IncompleteRead, ConnectionResetError, OSError):
            return e.code, {"raw": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return 0, {"error": f"connection failed: {e.reason}"}
    except (ConnectionResetError, http.client.IncompleteRead, OSError) as e:
        return 0, {"error": f"connection error: {e}"}


def test(description: str, ok: bool, detail: str = "") -> bool:
    """Print a single test result. Returns ok."""
    status = "PASS" if ok else "FAIL"
    msg = f"  [{status}] {description}"
    if not ok and detail:
        msg += f"\n         {detail}"
    print(msg)
    return ok


class TestSuite:
    def __init__(self, base_url: str, token: str, agent_id: int | None):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.agent = agent_id
        self.passed = 0
        self.failed = 0

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _req(self, path: str, data: dict) -> tuple[int, dict]:
        return post(self._url(path), data, self.headers)

    def _check(self, ok: bool):
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    def run(self):
        print("\n=== Saiga API Tests ===")
        print(f"   URL: {self.base}")
        print(f"   Agent ID: {self.agent or 'not set (agent tests skipped)'}")
        print()

        # First check if server is reachable
        code, _ = post(self._url("/api/agent/ping"), {}, self.headers)
        if code == 0:
            print("  [FAIL] Server is not reachable!")
            print("  Make sure the SS14 server is running with saiga enabled.")
            print(f"  Expected URL: {self._url('/api/agent/ping')}")
            sys.exit(1)
        if code == 404:
            print("  [FAIL] Endpoint /api/agent/ping returned 404.")
            print("  Make sure saiga.mcp.enabled=true and saiga.mcp.token is set.")
            sys.exit(1)

        self._test_ping()
        self._test_tools()

        if self.agent:
            self._test_observe()
            self._test_listen()
            self._test_say()
            self._test_act()
            self._test_recall()
            self._test_recipes()
            self._test_where_is()
        else:
            print("  [SKIP] agent tests (use --agent)")

        self._test_auth_invalid_token()
        self._test_auth_missing_token()
        self._test_auth_wrong_method()
        self._test_invalid_endpoint()
        self._test_invalid_json()

        total = self.passed + self.failed
        print(f"\n{'='*50}")
        print(f"Result: {self.passed}/{total} passed")
        if self.failed > 0:
            print(f"  [FAIL] {self.failed} tests failed!")
            sys.exit(1)
        else:
            print("  [PASS] All tests passed!")

    # ---- Tests ----

    def _test_ping(self):
        code, data = self._req("/api/agent/ping", {})
        self._check(test("ping returns 200", code == 200, f"code: {code}"))
        self._check(test("ping has ok=true", data.get("ok") is True, str(data)))

    def _test_tools(self):
        code, data = self._req("/api/agent/tools", {})
        self._check(test("tools returns 200", code == 200, f"code: {code}"))
        tools = data.get("tools", [])
        self._check(test("tools is not empty", len(tools) > 0, f"found {len(tools)} tools"))
        names = [t["name"] for t in tools]
        self._check(test("tools has observe", "observe" in names, str(names)))
        self._check(test("tools has act", "act" in names, str(names)))
        self._check(test("tools has say", "say" in names, str(names)))
        if tools:
            schema = tools[0].get("inputSchema", {})
            self._check(test("tool has inputSchema", bool(schema), str(tools[0].keys())))
            self._check(test("inputSchema has type=object", schema.get("type") == "object", str(schema)))
            self._check(test("inputSchema has properties", "properties" in schema, str(schema)))

    def _test_observe(self):
        code, data = self._req("/api/agent/observe", {"agent": self.agent})
        self._check(test("observe returns 200", code == 200, f"code: {code}"))
        self._check(test("observe has ok", data.get("ok") is True, str(data)))
        self._check(test("observe has entities", "entities" in data, str(data.keys())))
        self._check(test("observe has count", "count" in data, str(data.keys())))
        entities = data.get("entities", [])
        if entities:
            ent = entities[0]
            self._check(test("entity has id", "id" in ent, str(ent.keys())))
            self._check(test("entity has name", "name" in ent, str(ent.keys())))
            self._check(test("entity has dist", "dist" in ent, str(ent.keys())))
            self._check(test("entity has dir", "dir" in ent, str(ent.keys())))

        code_f, data_f = self._req("/api/agent/observe", {"agent": self.agent, "filter": "test"})
        self._check(test("observe with filter works", code_f == 200, str(data_f)))

    def _test_listen(self):
        code, data = self._req("/api/agent/listen", {"agent": self.agent})
        self._check(test("listen returns 200", code == 200, f"code: {code}"))
        self._check(test("listen has ok", data.get("ok") is True, str(data)))
        self._check(test("listen has heard", "heard" in data, str(data.keys())))

    def _test_say(self):
        code, data = self._req("/api/agent/say", {"agent": self.agent, "text": "Test say"})
        self._check(test("say returns 200", code == 200, f"code: {code}"))
        self._check(test("say has ok", data.get("ok") is True, str(data)))
        self._check(test("say has said", "said" in data, str(data.keys())))

        code_e, data_e = self._req("/api/agent/say", {"agent": self.agent, "text": ""})
        self._check(test("say with empty text returns error", "error" in data_e, str(data_e)))

        code_m, data_m = self._req("/api/agent/say", {"agent": self.agent})
        self._check(test("say without text returns error", "error" in data_m, str(data_m)))

    def _test_act(self):
        for action in ["stop", "drop", "swap"]:
            code, data = self._req("/api/agent/act", {"agent": self.agent, "action": action})
            self._check(test(f"act {action} returns ok", data.get("ok") is True, str(data)))

        code_e, data_e = self._req("/api/agent/act", {"agent": self.agent})
        self._check(test("act without action returns error", "error" in data_e, str(data_e)))

        code_b, data_b = self._req("/api/agent/act", {"agent": 999999, "action": "stop"})
        self._check(test("act with invalid agent returns error", "error" in data_b, str(data_b)))

    def _test_recall(self):
        code, data = self._req("/api/agent/recall", {"agent": self.agent})
        self._check(test("recall returns 200", code == 200, f"code: {code}"))
        self._check(test("recall has ok", data.get("ok") is True, str(data)))
        self._check(test("recall has nodes", "nodes" in data, str(data.keys())))

        code_q, data_q = self._req("/api/agent/recall", {"agent": self.agent, "query": "test"})
        self._check(test("recall with query works", code_q == 200, str(data_q)))

    def _test_recipes(self):
        code, data = self._req("/api/agent/recipes", {})
        self._check(test("recipes returns 200", code == 200, f"code: {code}"))
        self._check(test("recipes has ok", data.get("ok") is True, str(data)))
        self._check(test("recipes has recipes", "recipes" in data, str(data.keys())))

        code_q, data_q = self._req("/api/agent/recipes", {"query": "steel"})
        self._check(test("recipes with query works", code_q == 200, str(data_q)))

    def _test_where_is(self):
        code, data = self._req("/api/agent/where_is", {"agent": self.agent, "name": "test"})
        self._check(test("where_is returns 200", code == 200, f"code: {code}"))
        self._check(test("where_is has ok", data.get("ok") is True, str(data)))

        code_e, data_e = self._req("/api/agent/where_is", {"agent": self.agent})
        self._check(test("where_is without name returns error", "error" in data_e, str(data_e)))

    # -- Negative tests --

    def _test_auth_invalid_token(self):
        code, data = post(self._url("/api/agent/ping"), {}, {"Authorization": "Bearer wrong-token"})
        self._check(test("invalid token -> 401", code == 401, f"code: {code}"))

    def _test_auth_missing_token(self):
        code, data = post(self._url("/api/agent/ping"), {}, {})
        self._check(test("no token -> 401/404", code in (401, 404), f"code: {code}"))

    def _test_auth_wrong_method(self):
        req = urllib.request.Request(self._url("/api/agent/ping"), headers=self.headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5):
                code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = 0
        self._check(test("GET request -> 405", code == 405, f"code: {code}"))

    def _test_invalid_endpoint(self):
        code, data = self._req("/api/agent/nonexistent", {})
        self._check(test("unknown endpoint -> 500", code == 500, f"code: {code}"))

    def _test_invalid_json(self):
        url = self._url("/api/agent/ping")
        req = urllib.request.Request(url, data=b"not-json{{{", headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5):
                code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = 0
        self._check(test("invalid json -> 400", code == 400, f"code: {code}"))


def main():
    parser = argparse.ArgumentParser(description="Saiga API Tests")
    parser.add_argument("--base-url", default="http://127.0.0.1:1212")
    parser.add_argument("--token", default="devsecret")
    parser.add_argument("--agent", type=int, default=None)
    args = parser.parse_args()

    suite = TestSuite(args.base_url, args.token, args.agent)
    suite.run()


if __name__ == "__main__":
    main()
