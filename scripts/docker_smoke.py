"""Build the image and check the watcher, alerts, API, and dashboard end to end with Docker Compose."""

import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = f"monitor-smoke-{uuid4().hex[:12]}"
    environment = {**os.environ, "MONITOR_IMAGE": f"uptime-monitor-smoke:{project}"}
    compose = ["docker", "compose", "--project-name", project, "--file", str(ROOT / "compose.yml"),
               "--file", str(ROOT / "tests" / "compose.smoke.yml")]

    def run(arguments, expected=0, timeout=120):
        result = subprocess.run(compose + arguments, cwd=ROOT, env=environment,
                                text=True, capture_output=True, timeout=timeout)
        if result.returncode != expected:
            raise RuntimeError(f"{arguments}: expected exit {expected}, got {result.returncode}\n"
                               f"{result.stdout}\n{result.stderr}")
        return result.stdout

    def command(*arguments, expected=0):
        return run(["run", "--rm", "--no-deps", "monitor", *arguments], expected)

    try:
        print("Building the monitor image...", flush=True)
        run(["build", "monitor"], timeout=300)
        run(["up", "--detach", "--wait", "--wait-timeout", "30", "fixture"])
        uid = run(["run", "--rm", "--no-deps", "--entrypoint", "id", "monitor", "-u"]).strip()
        assert uid == "10001", uid
        command("add", "service", "http://fixture:8000/service", "--timeout", "2")
        webhook = "http://fixture:8000/webhook"
        output = command("watch", "--interval", "1", "--count", "4", "--alert-webhook", webhook, expected=1)
        batches = [json.loads(line) for line in output.splitlines()]
        assert [batch["cycle"] for batch in batches] == [1, 2, 3, 4], batches
        assert [batch["results"][0]["state"] for batch in batches] == ["down", "down", "up", "up"], batches
        incident, = json.loads(command("incidents"))
        assert incident["state"] == "resolved", incident
        assert incident["failed_checks"] == 2, incident
        assert incident["resolution_check_id"] == batches[-1]["results"][0]["id"], incident
        assert len(json.loads(command("history"))) == 4
        summary, = json.loads(command("summary", "--url", "http://fixture:8000/service"))
        assert (summary["stored_checks"], summary["up_checks"], summary["down_checks"]) == (4, 2, 2), summary
        assert summary["latest_state"] == "up" and summary["latest_error"] is None, summary
        recent, = json.loads(command("summary", "--last", "2"))
        assert recent["sampled_checks"] == 2 and recent["down_checks"] == 0, recent
        assert recent["stored_checks"] == 4, recent
        assert recent["up_min_latency_ms"] <= recent["up_avg_latency_ms"] <= recent["up_max_latency_ms"], recent
        assert len(json.loads(command("history"))) == 4
        print("Failure, recovery, and persistence across containers passed.", flush=True)

        command("watch", "--interval", "1", "--count", "1", "--alert-webhook", webhook)
        received = json.loads(run(["exec", "-T", "fixture", "python", "-c", "import urllib.request; "
                                   "print(urllib.request.urlopen('http://127.0.0.1:8000/alerts').read().decode())"]))
        assert [(alert["event"], alert["url"]) for alert in received] == [
            ("opened", "http://fixture:8000/service"), ("resolved", "http://fixture:8000/service")], received
        assert received[1]["incident"]["resolution_check_id"] == incident["resolution_check_id"], received
        logged = json.loads(command("alerts"))
        assert [(row["event"], row["status"]) for row in logged] == [("resolved", "sent"), ("opened", "sent")], logged
        keys = [alert["idempotency_key"] for alert in received]
        assert keys == [alert["header_key"] for alert in received] and len(set(keys)) == 2, received
        assert [row["idempotency_key"] for row in logged] == keys[::-1], logged
        print("Webhook alerts sent once across restarts, with matching keys, passed.", flush=True)

        run(["up", "--detach", "--no-deps", "monitor"])
        deadline = time.monotonic() + 20
        while True:
            logs = run(["logs", "--no-log-prefix", "monitor"])
            if '"cycle": 1' in logs:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Watcher did not produce its first batch: {logs}")
            time.sleep(0.2)
        run(["stop", "--timeout", "5", "monitor"])
        container = run(["ps", "--all", "--quiet", "monitor"]).strip()
        inspected = subprocess.run(["docker", "inspect", container], text=True, capture_output=True,
                                   check=True, timeout=30)
        state = json.loads(inspected.stdout)[0]["State"]
        assert state["ExitCode"] == 143 and not state["Running"], state
        assert len(json.loads(command("history"))) == 6
        assert json.loads(command("incidents")) == [incident]
        print("SIGTERM shutdown and saved history passed.", flush=True)

        # The fixture serves one request at a time, so this runs after the watcher checks.
        slow = json.loads(command("check", "http://fixture:8000/slow", "--timeout", "10", "--deadline", "1",
                                  expected=1))
        assert slow["error"] == "deadline_exceeded" and slow["latency_ms"] < 3000, slow
        print("Request deadline passed.", flush=True)

        run(["run", "--detach", "--no-deps", "--name", f"{project}-api", "monitor",
             "serve", "--host", "0.0.0.0", "--port", "8080"])
        fetch = ("import json, sys, time, urllib.request, urllib.error\n"
                 "for attempt in range(50):\n"
                 "    try:\n"
                 "        response = urllib.request.urlopen(sys.argv[1], timeout=2)\n"
                 "        break\n"
                 "    except urllib.error.URLError:\n"
                 "        time.sleep(0.2)\n"
                 "print(response.read().decode())")
        api = f"http://{project}-api:8080/api"
        served, = json.loads(run(["exec", "-T", "fixture", "python", "-c", fetch, api + "/summary?target=service"]))
        assert (served["stored_checks"], served["latest_state"]) == (6, "up"), served
        assert json.loads(run(["exec", "-T", "fixture", "python", "-c", fetch, api + "/incidents"])) == [incident]
        page = run(["exec", "-T", "fixture", "python", "-c", fetch, f"http://{project}-api:8080/"])
        assert "<title>Uptime Monitor</title>" in page and "/api/summary" in page, page[:200]
        assert len(json.loads(command("history", "--limit", "100"))) == 7
        print("Read-only API and dashboard passed.", flush=True)

        # An incident that opens while no alerting watcher runs is sent when the next one starts.
        command("add", "missing", "http://fixture:8000/missing", "--timeout", "2")
        command("run", "missing", expected=1)
        command("run", "missing", expected=1)
        pending, = json.loads(command("alerts", "--pending"))
        assert (pending["event"], pending["url"]) == ("opened", "http://fixture:8000/missing"), pending
        command("watch", "--interval", "1", "--count", "1", "--alert-webhook", webhook, expected=1)
        received = json.loads(run(["exec", "-T", "fixture", "python", "-c", fetch, "http://127.0.0.1:8000/alerts"]))
        assert [(alert["event"], alert["url"]) for alert in received[2:]] == [
            ("opened", "http://fixture:8000/missing")], received
        assert received[2]["incident"] == pending["incident"], received
        assert received[2]["idempotency_key"] == received[2]["header_key"] == pending["idempotency_key"], received
        assert json.loads(command("alerts", "--pending")) == []
        print("Alerts missed while no watcher was running passed.", flush=True)
    finally:
        subprocess.run(["docker", "rm", "--force", f"{project}-api"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        run(["down", "--volumes", "--remove-orphans"])
        subprocess.run(["docker", "image", "rm", environment["MONITOR_IMAGE"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)


if __name__ == "__main__":
    main()
