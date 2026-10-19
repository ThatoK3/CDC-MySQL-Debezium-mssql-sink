"""Command line for the Kafka Connect side:  python -m pipeline.connectctl <command>

  render                 print every connector configuration with the environment filled in (secrets masked)
  apply [--group G]      create or update the connectors (PUT /connectors/<name>/config — safe to repeat)
  status                 state of every connector and task
  pause|resume [--group G]
  delete [--group G]
  check                  verify the environment has every value the configurations need

Groups: load (every scheduled load), daily (first load of the day), enrolment (HealthID/PHP/Workflow, always on, polled every ENROL_POLL_MS), sink (landing sinks, always on), writeback (PostgreSQL -> SQL Server, always on).
Read-side sources are applied PAUSED so SQL Server is only touched at the scheduled times.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from . import spec

PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
SECRET = re.compile(r"password|secret", re.I)
GROUPS = ("load", "daily", "enrolment", "sink", "writeback")
READ_GROUPS = ("load", "daily")


def render_value(value, values, missing):
    def sub(m):
        if m.group(1) in values:
            return values[m.group(1)]
        missing.add(m.group(1))
        return m.group(0)
    return PLACEHOLDER.sub(sub, value)


def render(env=None, prefix="mlp", groups=None):
    """{name: (group, config)} with every ${NAME} replaced. Returns (connectors, missing names)."""
    values, missing, out = spec.env_values(env), set(), {}
    for name, (group, cfg) in spec.all_connectors(prefix).items():
        if groups and group not in groups:
            continue
        out[name] = (group, {k: render_value(v, values, missing) for k, v in cfg.items()})
    return out, sorted(missing)


class Connect:
    def __init__(self, url=None, timeout=30):
        self.url, self.timeout = (url or os.environ.get("CONNECT_URL", "http://localhost:8083")).rstrip("/"), timeout

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError(f"{method} {path}: HTTP {exc.code} {exc.read().decode()[:300]}") from exc

    def put_config(self, name, config):
        return self.call("PUT", f"/connectors/{name}/config", config)

    def status(self, name):
        return self.call("GET", f"/connectors/{name}/status")

    def pause(self, name):
        return self.call("PUT", f"/connectors/{name}/pause")

    def resume(self, name):
        return self.call("PUT", f"/connectors/{name}/resume")

    def restart(self, name):
        """Restart the connector and its tasks (a bulk source polls again at once instead of waiting out its interval)."""
        return self.call("POST", f"/connectors/{name}/restart?includeTasks=true&onlyFailed=false")

    def delete(self, name):
        return self.call("DELETE", f"/connectors/{name}")

    def wait_ready(self, seconds=120):
        end = time.time() + seconds
        while time.time() < end:
            try:
                if self.call("GET", "/") is not None:
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
        return False


def apply(connect, connectors, paused_groups=READ_GROUPS):
    for name, (group, cfg) in connectors.items():
        connect.put_config(name, cfg)
        if group in paused_groups:
            connect.pause(name)             # created paused: the scheduler decides when SQL Server is read
        print(f"applied {name} ({group})" + (" — paused" if group in paused_groups else ""))


def state_of(status):
    if not status:
        return "MISSING"
    tasks = [t["state"] for t in status.get("tasks", [])]
    if "FAILED" in tasks:
        return "FAILED"
    return status["connector"]["state"] if not tasks or all(t == status["connector"]["state"] for t in tasks) else status["connector"]["state"] + "/" + ",".join(sorted(set(tasks)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="connectctl")
    ap.add_argument("command", choices=["render", "apply", "status", "pause", "resume", "delete", "check"])
    ap.add_argument("--group", action="append", choices=GROUPS)
    ap.add_argument("--url")
    a = ap.parse_args(argv)
    connectors, missing = render(groups=a.group)
    if a.command == "check":
        print("missing: " + ", ".join(missing) if missing else "environment complete")
        return 1 if missing else 0
    if a.command == "render":
        for name, (group, cfg) in connectors.items():
            print(json.dumps({"name": name, "group": group, "config": {k: ("***" if SECRET.search(k) else v) for k, v in cfg.items()}}, indent=2))
        return 0
    if missing and a.command == "apply":
        print("Cannot apply, these values are not set: " + ", ".join(missing), file=sys.stderr)
        return 1
    c = Connect(a.url)
    if a.command == "apply":
        if not c.wait_ready():
            print("Kafka Connect is not reachable at " + c.url, file=sys.stderr)
            return 1
        apply(c, connectors)
        return 0
    bad = 0
    for name, (group, _) in connectors.items():
        if a.command == "status":
            st = state_of(c.status(name))
            bad += st in ("FAILED", "MISSING")
            print(f"{name:34} {group:10} {st}")
        else:
            getattr(c, a.command)(name)
            print(f"{a.command}: {name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
