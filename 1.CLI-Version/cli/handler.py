import json
import os
import re

from utils.logger import get_logger
from utils.paths import CONFIG_PATH, resolve_project_path
from utils.process_manager import ProcessManager
from utils.paths import ENGINE_STATUS_PATH
from utils.runtime_status import read_runtime_snapshot, SnapshotRead

logger = get_logger()


def load_config_for_status():
    # Optional display data; status and stop do not require valid workflow config.
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as file:
            config = json.load(file)
        if not isinstance(config, dict):
            return None
        if "tasks" in config:
            if type(config.get("schema_version")) is not int or config["schema_version"] != 2 or not isinstance(config["tasks"], list):
                return None
            for task in config["tasks"]:
                if not isinstance(task, dict) or not isinstance(task.get("trigger"), dict):
                    return None
                if not isinstance(task.get("enabled"), bool) or not isinstance(task.get("id"), str):
                    return None
                if not re.fullmatch(r"[a-z0-9_-]+", task["id"]):
                    return None
                trigger_type = task["trigger"].get("type")
                if not isinstance(trigger_type, str) or trigger_type not in ("daily", "interval", "file_event"):
                    return None
        else:
            config["watch_folder"] = str(resolve_project_path(config["watch_folder"]))
        return config
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _show_configuration():
    config = load_config_for_status()
    print("\n  Current configuration on disk (not live task/worker status):")
    if config is None:
        print("    Workflow configuration is unavailable or invalid.")
        return
    if "tasks" in config:
        print(f"    Configured tasks: {len(config['tasks'])}")
        for task in config["tasks"]:
            # Show only safe identity/trigger fields, never arbitrary task data.
            label = task["id"]
            password = os.environ.get("SMTP_PASSWORD")
            if password:
                label = label.replace(password, "[REDACTED]")
            print(f"      Task {label!r}: enabled={task['enabled']}, trigger={task['trigger']['type']!r}")
        print("    Configuration changes require engine restart.")
        return
    print(f"    Watching: {config['watch_folder']}")
    try:
        hour = config["schedule"]["hour"]
        minute = config["schedule"]["minute"]
        print(f"    Report time: {hour:02d}:{minute:02d} daily")
    except (KeyError, TypeError, ValueError):
        print("    Report time: unavailable or invalid")


def _show_runtime(observation):
    if observation.snapshot is None:
        if observation.age is not None:
            print(f"  Snapshot age: {observation.age:.1f} seconds (stale)")
        print("  Runtime details unavailable: " + observation.reason)
        return
    data = observation.snapshot
    print(f"  Snapshot age: {observation.age:.1f} seconds")
    print(f"  Observed lifecycle: {data['lifecycle']}")
    print("\n  Workers (thread alive does not prove responsiveness):")
    print(f"    Scheduler: {data['scheduler']}")
    monitors = data["monitors"]
    alive = sum(worker["state"] == "alive" for worker in monitors)
    print(f"    Monitors: {alive}/{len(monitors)} alive")
    for worker in monitors:
        print(f"      {worker['label']}: {worker['state']}")
    if data["lifecycle"] == "RUNNING" and (data["scheduler"] != "alive" or alive != len(monitors)):
        print("    Warning: one or more expected workers are stopped or unavailable.")
    print(f"\n  Tasks loaded: {data['enabled_count']} enabled / {data['configured_count']} configured")
    print(f"  Queued events: approximately {data['queued_events']}")
    current = data["current"]
    if current is None:
        print("  Current execution: none (idle)")
    else:
        print(f"  Current execution: {current['id']} [{current['type']}]")
        print(f"    Source: {current['source']} | Phase: {current['phase']}")
        print(f"    Started: {current['started_at']}")
    last = data["last_result"]
    if last is None:
        print("  Last finalized result: none")
    else:
        print(f"  Last finalized result: {last['id']} - {last['status']}")
        print(f"    Finished: {last['finished_at']}")
    print("\n  Tasks loaded by this instance (schedules, not exact next-run times):")
    for task in data["tasks"]:
        enabled = "enabled" if task["enabled"] else "disabled"
        print(f"    {task['id']} | {task['type']} | {task['schedule']} | {enabled}")


def handle_command(command, engine=None, process_manager=None, stop_timeout=5.0):
    manager = process_manager if process_manager is not None else ProcessManager()
    if command == "start":
        logger.info("Command received: START")
        if engine is None:
            print("No engine was prepared for start.")
            return 1
        return engine.start()
    if command == "stop":
        logger.info("Command received: STOP")
        result = manager.request_stop(timeout=stop_timeout)
        print(result.detail)
        return 0 if result.state == "STOPPED" else 1
    if command == "status":
        status = manager.inspect_status()
        observation = None
        if status.metadata is not None:
            metadata = status.metadata
            observation = read_runtime_snapshot(manager.runtime_dir / ENGINE_STATUS_PATH.name, metadata)
            current = manager.inspect_status()
            if (current.metadata is None or current.metadata["instance_id"] != metadata["instance_id"]
                    or current.metadata["pid"] != metadata["pid"]):
                observation = SnapshotRead(reason="Engine ownership changed during reading")
            status = current
        print("\n========================================")
        print("       AUTOMATION ENGINE - STATUS       ")
        print("========================================")
        pid = f" (PID: {status.metadata['pid']})" if status.metadata else ""
        print(f"  Engine status: {status.state}{pid}")
        if status.detail:
            print("  " + status.detail)
        if status.metadata:
            print("  Instance: " + status.metadata["instance_id"])
            if observation is not None:
                _show_runtime(observation)
            _show_configuration()
        elif status.state == "STOPPED":
            print("  No engine instance owns the project lock.")
        print("========================================\n")
        return 0 if status.state not in {"UNKNOWN", "LEGACY"} else 1
    print("Unknown command. Available commands:")
    print("  python main.py start")
    print("  python main.py stop")
    print("  python main.py status")
    return 2
