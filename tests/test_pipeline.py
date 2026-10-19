import datetime
import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from pipeline import connectctl, scheduler, spec

ROOT = Path(__file__).resolve().parent.parent
ENV = {k: "x" for k in ("MSSQL_HOST", "MSSQL_DB", "MSSQL_READ_USER", "MSSQL_READ_PASSWORD", "MSSQL_INBOX_USER", "MSSQL_INBOX_PASSWORD", "LANDING_HOST", "LANDING_DB",
                         "LANDING_USER", "LANDING_PASSWORD", "APP_PG_HOST", "APP_PG_DB", "APP_PG_REPLICATION_USER", "APP_PG_REPLICATION_PASSWORD")}


class Rendering(unittest.TestCase):
    def test_a_complete_environment_leaves_no_placeholder(self):
        connectors, missing = connectctl.render(ENV)
        self.assertEqual(missing, [])
        for name, (_, cfg) in connectors.items():
            self.assertNotIn("${", json.dumps(cfg), name)

    def test_missing_values_are_named(self):
        _, missing = connectctl.render({})
        self.assertIn("MSSQL_HOST", missing)
        self.assertIn("LANDING_PASSWORD", missing)

    def test_programme_filter_and_window(self):
        env = {**ENV, "PROGRAMME_CODES": "1001, 22", "RETENTION_DAYS": "200"}
        q = connectctl.render(env)[0]["mlp-src-eligibility"][1]["query"]
        self.assertIn("PROGRAMME_CODE IN (1001,22)", q)
        self.assertIn("DATEADD(day, -200,", q)
        with self.assertRaises(ValueError):
            spec.env_values({"PROGRAMME_CODES": "1001; DROP TABLE x"})

    def test_every_source_has_one_sink_on_the_same_topic(self):
        connectors, _ = connectctl.render(ENV)
        src = {c["topic.prefix"] for g, c in connectors.values() if c["connector.class"].endswith("JdbcSourceConnector")}
        sink = {t for g, c in connectors.values() if "topics" in c and c["connector.class"].endswith("JdbcSinkConnector") and c["topics"].startswith("ml.read.") for t in c["topics"].split(",")}
        self.assertEqual(src, sink)

    def test_sources_read_only_what_changed_by_update_then_load_date(self):
        c = connectctl.render(ENV)[0]["mlp-src-eligibility"][1]
        self.assertEqual((c["mode"], c["timestamp.column.name"], c["incrementing.column.name"]), ("timestamp+incrementing", "UPDATE_DATE,LOAD_DATE", "ID"))
        self.assertEqual(c["connection.url"].count("applicationIntent=ReadOnly"), 1)
        self.assertTrue(int(c["timestamp.initial"]) > 0)

    def test_sources_never_read_identity_numbers_or_birth_dates(self):
        text = json.dumps(connectctl.render(ENV)[0])
        for forbidden in ("ID_NUMBER", "DATE_OF_BIRTH", "LEGAL"):
            self.assertNotIn(forbidden, text)

    def test_the_inbox_login_and_the_read_login_are_different(self):
        connectors, _ = connectctl.render({**ENV, "MSSQL_READ_USER": "reader", "MSSQL_INBOX_USER": "inbox"})
        users = {name: c["connection.user"] for name, (g, c) in connectors.items() if "mssql" in c.get("connection.url", "") or "sqlserver" in c.get("connection.url", "")}
        self.assertEqual({v for k, v in users.items() if "-src-" in k}, {"reader"})
        self.assertEqual({v for k, v in users.items() if "-sink-" in k}, {"inbox"})

    def test_write_back_is_idempotent_by_key_and_never_logs_messages(self):
        connectors, _ = connectctl.render(ENV)
        for name, (group, c) in connectors.items():
            if group == "writeback" and "Sink" in c["connector.class"]:
                self.assertEqual((c["insert.mode"], c["pk.fields"]), ("upsert", "outbox_id"))
                self.assertEqual(c["errors.log.include.messages"], "false")
        wb = connectors["mlp-cdc-writeback"][1]
        self.assertEqual(wb["publication.autocreate.mode"], "disabled")
        self.assertEqual(wb["snapshot.mode"], "no_data")


class Schemas(unittest.TestCase):
    def landing_columns(self):
        text = (ROOT / "sql/postgres/landing.sql").read_text()
        out = {}
        for m in re.finditer(r'CREATE TABLE IF NOT EXISTS landing\."(\w+)" \((.*?)\);\s*\n', text, re.S):
            out[m.group(1)] = set(re.findall(r'"(\w+)"\s+(?:bigint|integer|varchar|double|timestamp|text)', m.group(2)))
        return out

    def test_landing_has_every_column_the_connectors_write(self):
        landing = self.landing_columns()
        for r in spec.READS:
            table = f"{r.schema}__{r.table}"
            self.assertIn(table, landing)
            self.assertTrue(set(r.columns) <= landing[table], f"{table} lacks {set(r.columns) - landing[table]}")
            self.assertTrue(set(r.pk) <= set(r.columns))

    def test_landing_uses_the_names_the_app_loader_asks_for(self):
        self.assertIn("ML_LEAD__VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY", self.landing_columns())

    def test_inbox_tables_match_what_the_app_stages(self):
        text = (ROOT / "sql/sqlserver/cdc_inbox.sql").read_text()
        staged = {  # the columns of apps/sync/models.py Staged* in the platform repository
            "NOTE": ["outbox_id", "eligibility_id", "programme_code", "staged_at", "username", "notes", "load_date"],
            "ASSIGNMENT": ["outbox_id", "eligibility_id", "programme_code", "staged_at", "assignee_entity_no", "assigned_by_entity_no", "load_date", "assignment_batch_no"],
            "FIELD": ["outbox_id", "eligibility_id", "programme_code", "staged_at", "field_name", "field_value", "load_date", "field_control_batch_no"],
            "STATUS": ["outbox_id", "eligibility_id", "programme_code", "staged_at", "status", "updated_by_entity_no", "requested_at"],
        }
        for table, cols in staged.items():
            block = re.search(rf"CREATE TABLE CDC_IN\.{table} \((.*?)\);", text, re.S).group(1)
            for c in cols:
                self.assertIn(c + " ", block, f"CDC_IN.{table} lacks {c}")
        self.assertEqual([w.inbox for w in spec.WRITES], [f"CDC_IN.{t}" for t in staged])


class FakeConnect(BaseHTTPRequestHandler):
    calls = []

    def _reply(self, code=200, body=None):
        data = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.calls.append(("GET", self.path))
        self._reply(200, {"version": "x"}) if self.path == "/" else self._reply(404)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.calls.append(("PUT", self.path, json.loads(self.rfile.read(n) or b"null")))
        self._reply(200 if not self.path.endswith(("pause", "resume")) else 202)

    def log_message(self, *a):
        pass


class Applying(unittest.TestCase):
    def test_apply_puts_every_connector_and_pauses_only_the_read_side(self):
        FakeConnect.calls = []
        server = HTTPServer(("127.0.0.1", 0), FakeConnect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            connectors, _ = connectctl.render(ENV)
            connectctl.apply(connectctl.Connect(f"http://127.0.0.1:{server.server_port}"), connectors)
        finally:
            server.shutdown()
        puts = [c for c in FakeConnect.calls if c[0] == "PUT" and c[1].endswith("/config")]
        pauses = [c[1] for c in FakeConnect.calls if c[1].endswith("/pause")]
        self.assertEqual(len(puts), len(connectors))
        self.assertEqual(sorted(p.split("/")[2] for p in pauses), sorted(n for n, (g, _) in connectors.items() if g in ("load", "daily")))
        self.assertFalse([p for p in pauses if "sink" in p or "writeback" in p])


class FakeConnectApi:
    def __init__(self, failed=()):
        self.log, self.failed = [], set(failed)

    def resume(self, n): self.log.append(("resume", n))
    def pause(self, n): self.log.append(("pause", n))
    def restart(self, n): self.log.append(("restart", n))

    def status(self, n):
        return {"connector": {"state": "RUNNING"}, "tasks": [{"state": "FAILED" if n in self.failed else "RUNNING"}]}


class Probe:
    def __init__(self, offsets, lag=0):
        self.offsets, self.lag_value, self.i = list(offsets), lag, 0

    def end_offsets(self, topics):
        v = self.offsets[min(self.i, len(self.offsets) - 1)]
        self.i += 1
        return v

    def lag(self, sink, topic):
        return self.lag_value


class Landing:
    def __init__(self): self.calls = []

    def finish_reconcile(self, since):
        self.calls.append(since)
        return 7


class Clock:
    def __init__(self): self.t = 0

    def __call__(self): return self.t
    def sleep(self, s): self.t += s


class Window(unittest.TestCase):
    def make(self, offsets, failed=(), lag=0, **kw):
        clock, api, landing = Clock(), FakeConnectApi(failed), Landing()
        s = scheduler.Scheduler(api, Probe(offsets, lag), landing, poll=15, idle_seconds=60, min_run=30, max_run=600, sink_wait=120, clock=clock, sleep=clock.sleep, **kw)
        return s, api, landing

    def test_resumes_waits_until_idle_then_pauses(self):
        s, api, landing = self.make([0, 10, 50, 50, 50, 50, 50, 50, 50])
        r = s.run_window()
        self.assertEqual(r["state"], "ok")
        names = [n for _, n in api.log]
        resumed = [n for a, n in api.log if a == "resume"]
        self.assertEqual(len(resumed), len([x for x in spec.READS if x.group == "load"]))
        self.assertEqual(api.log[-len(resumed):], [("pause", n) for n in resumed])        # paused again at the end
        self.assertNotIn("mlp-src-reconcile", names)                                      # daily connectors stay off in an ordinary window
        self.assertEqual(landing.calls, [])

    def test_the_first_window_of_the_day_reconciles(self):
        s, api, landing = self.make([5] * 20)
        r = s.run_window(first_of_day=True)
        self.assertEqual((r["state"], r["reconciled"]), ("ok", 7))
        self.assertIn(("restart", "mlp-src-reconcile"), api.log)                         # a bulk source polls again at once
        self.assertEqual(len(landing.calls), 1)

    def test_a_window_that_never_settles_is_paused_at_the_limit(self):
        s, api, _ = self.make(list(range(0, 10_000, 7)))
        r = s.run_window()
        self.assertEqual(r["state"], "timeout")
        self.assertEqual([a for a, _ in api.log[-2:]], ["pause", "pause"])

    def test_a_failed_connector_stops_the_window_and_still_pauses(self):
        s, api, landing = self.make([1, 2, 3], failed={"mlp-src-staff"})
        r = s.run_window(first_of_day=True)
        self.assertEqual(r["state"], "failed")
        self.assertEqual(landing.calls, [])                                               # nothing is marked not-current after a failed pass
        self.assertIn(("pause", "mlp-src-eligibility"), api.log)

    def test_sinks_that_have_not_caught_up_delay_the_close(self):
        s, api, _ = self.make([5] * 30, lag=3)
        self.assertEqual(s.run_window()["state"], "timeout")


class Times(unittest.TestCase):
    def test_next_slot(self):
        times = scheduler.parse_times("17:00,06:00,12:00")
        wait, at = scheduler.seconds_until(times, datetime.datetime(2026, 10, 19, 11, 0))
        self.assertEqual((at, wait), (datetime.datetime(2026, 10, 19, 12, 0), 3600))
        _, at = scheduler.seconds_until(times, datetime.datetime(2026, 10, 19, 17, 30))
        self.assertEqual(at, datetime.datetime(2026, 10, 20, 6, 0))


if __name__ == "__main__":
    unittest.main()
