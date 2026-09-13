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
    finally:
        run(["down", "--volumes", "--remove-orphans"])
        subprocess.run(["docker", "image", "rm", environment["MONITOR_IMAGE"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)


if __name__ == "__main__":
    main()
