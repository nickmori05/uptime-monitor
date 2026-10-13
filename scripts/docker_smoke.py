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
        output = command("watch", "--interval", "1", "--count", "4", expected=1)
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
        inspected = subprocess.run(["docker", "inspect", container], text=True, capture_output=True, check=True, timeout=30)
        state = json.loads(inspected.stdout)[0]["State"]
        assert state["ExitCode"] == 143 and not state["Running"], state
        assert len(json.loads(command("history"))) == 5
        assert json.loads(command("incidents")) == [incident]
        print("SIGTERM shutdown and saved history passed.", flush=True)

        # The fixture serves one request at a time, so this runs after the watcher checks.
        slow = json.loads(command("check", "http://fixture:8000/slow", "--timeout", "10", "--deadline", "1", expected=1))
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
        assert (served["stored_checks"], served["latest_state"]) == (5, "up"), served
        assert json.loads(run(["exec", "-T", "fixture", "python", "-c", fetch, api + "/incidents"])) == [incident]
        assert len(json.loads(command("history", "--limit", "100"))) == 6
        print("Read-only API passed.", flush=True)
    finally:
        subprocess.run(["docker", "rm", "--force", f"{project}-api"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        run(["down", "--volumes", "--remove-orphans"])
        subprocess.run(["docker", "image", "rm", environment["MONITOR_IMAGE"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)


if __name__ == "__main__":
    main()
