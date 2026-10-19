"""Opens SQL Server for reading only at the scheduled times.

At each time in SYNC_TIMES (default 06:00, 12:00, 17:00, local): resume the read connectors, let them pull what changed (UPDATE_DATE / LOAD_DATE past
their offsets), wait until nothing new is arriving AND the landing sinks have caught up, then pause them again. The first window of the day also
runs the `daily` connectors (the status/rank reconcile) and then marks leads that were not seen as no longer current. A window that cannot finish
in MAX_RUN_MINUTES pauses anyway and carries on next time (offsets are kept). `python -m pipeline.scheduler --now` runs one window immediately.
"""
import argparse
import datetime
import logging
import os
import sys
import time

from . import connectctl, spec

log = logging.getLogger("scheduler")


class KafkaProbe:
    """Topic end offsets and how far the sink consumer groups have read (confluent-kafka, imported only when used)."""

    def __init__(self, bootstrap=None):
        from confluent_kafka import Consumer
        self._Consumer = Consumer
        self.bootstrap = bootstrap or os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")

    def _consumer(self, group):
        return self._Consumer({"bootstrap.servers": self.bootstrap, "group.id": group, "enable.auto.commit": False})

    def end_offsets(self, topics):
        from confluent_kafka import TopicPartition
        c = self._consumer("mlp-scheduler-probe")
        try:
            total = 0
            for t in topics:
                meta = c.list_topics(t, timeout=10).topics.get(t)
                if not meta or meta.error:
                    continue
                for p in meta.partitions:
                    total += c.get_watermark_offsets(TopicPartition(t, p), timeout=10)[1]
            return total
        finally:
            c.close()

    def lag(self, sink_name, topic):
        """Messages the sink connector has not consumed yet (its group is connect-<name>)."""
        from confluent_kafka import TopicPartition
        c = self._consumer("connect-" + sink_name)
        try:
            meta = c.list_topics(topic, timeout=10).topics.get(topic)
            if not meta or meta.error:
                return 0
            parts = [TopicPartition(topic, p) for p in meta.partitions]
            lag = 0
            for tp, committed in zip(parts, c.committed(parts, timeout=10)):
                high = c.get_watermark_offsets(tp, timeout=10)[1]
                lag += max(high - (committed.offset if committed.offset >= 0 else 0), 0)
            return lag
        finally:
            c.close()


class Landing:
    """The landing database (psycopg, imported only when used)."""

    def finish_reconcile(self, since):
        import psycopg
        dsn = f"host={os.environ['LANDING_HOST']} port={os.environ.get('LANDING_PORT', '5432')} dbname={os.environ['LANDING_DB']} user={os.environ['LANDING_USER']} password={os.environ['LANDING_PASSWORD']}"
        with psycopg.connect(dsn) as conn:
            return conn.execute("SELECT landing.finish_reconcile(%s)", [since]).fetchone()[0]


def parse_times(text):
    out = []
    for part in (text or "06:00,12:00,17:00").split(","):
        h, m = part.strip().split(":")
        out.append(datetime.time(int(h), int(m)))
    return sorted(out)


class Scheduler:
    def __init__(self, connect, probe, landing, *, prefix="mlp", poll=15, idle_seconds=90, min_run=120, max_run=3600, sink_wait=1800,
                 clock=time.time, sleep=time.sleep, groups_source=None):
        self.connect, self.probe, self.landing, self.prefix = connect, probe, landing, prefix
        self.poll, self.idle_seconds, self.min_run, self.max_run, self.sink_wait = poll, idle_seconds, min_run, max_run, sink_wait
        self.clock, self.sleep = clock, sleep
        self.reads = {r.name: r for r in spec.READS}

    def sources(self, groups):
        return [(r, spec.read_source(r, self.prefix)[0], spec.landing_sink(r, self.prefix)[0]) for r in spec.READS if r.group in groups]

    def run_window(self, first_of_day=False):
        """One load window. Returns {"state": ok|timeout|failed, "seconds": n, "sources": [...], "reconciled": n|None}."""
        groups = {"load", "daily"} if first_of_day else {"load"}
        picked = self.sources(groups)
        topics = [r.topic for r, _, _ in picked]
        started = self.clock()
        stamp = datetime.datetime.now(datetime.timezone.utc)
        log.info("window opens: %s", ", ".join(n for _, n, _ in picked))
        for r, src, _ in picked:
            self.connect.resume(src)
            if r.mode == "bulk":
                self.connect.restart(src)
        result = {"state": "ok", "sources": [n for _, n, _ in picked], "reconciled": None}
        try:
            result["state"] = self._wait(picked, topics, started)
        finally:
            for _, src, _ in picked:
                self.connect.pause(src)
        if result["state"] == "ok" and first_of_day and any(r.name == "reconcile" for r, _, _ in picked):
            result["reconciled"] = self.landing.finish_reconcile(stamp)
        result["seconds"] = round(self.clock() - started)
        log.info("window closed: %s in %ss", result["state"], result["seconds"])
        return result

    def _wait(self, picked, topics, started):
        last, last_change = None, started
        while True:
            self.sleep(self.poll)
            now = self.clock()
            for _, src, _ in picked:
                st = self.connect.status(src)
                if connectctl.state_of(st) == "FAILED":
                    log.error("%s failed: %s", src, st)
                    return "failed"
            total = self.probe.end_offsets(topics)
            if total != last:
                last, last_change = total, now
            if now - started >= self.max_run:
                return "timeout"
            if now - started >= self.min_run and now - last_change >= self.idle_seconds:
                break
        deadline = self.clock() + self.sink_wait
        while self.clock() < deadline:                                   # the sources are idle; let the landing sinks drain what they produced
            if all(self.probe.lag(sink, r.topic) == 0 for r, _, sink in picked):
                return "ok"
            self.sleep(self.poll)
        return "timeout"


def seconds_until(times, now):
    """Seconds to the next scheduled time after `now` (a datetime), and that time."""
    for day in (0, 1):
        for t in times:
            at = datetime.datetime.combine(now.date() + datetime.timedelta(days=day), t)
            if at > now:
                return (at - now).total_seconds(), at
    raise ValueError("no times")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="run one window immediately and exit")
    ap.add_argument("--first", action="store_true", help="with --now: include the daily connectors")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = spec.env_values()
    s = Scheduler(connectctl.Connect(), KafkaProbe(), Landing(), idle_seconds=int(env.get("IDLE_SECONDS", 90)), max_run=int(env.get("MAX_RUN_MINUTES", 60)) * 60)
    if a.now:
        print(s.run_window(first_of_day=a.first))
        return 0
    times = parse_times(env.get("SYNC_TIMES"))
    done_today = None
    while True:
        wait, at = seconds_until(times, datetime.datetime.now())
        log.info("next window %s", at)
        time.sleep(max(wait, 0))
        first = done_today != at.date()
        s.run_window(first_of_day=first)
        done_today = at.date()


if __name__ == "__main__":
    sys.exit(main())
