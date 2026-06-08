import subprocess
import time
import json
import os
import shutil
import re

from flask import Flask, jsonify, request

app = Flask(__name__)

# ============================================================
# Global paths
# ============================================================

PYTHON_EXE = sys.executable
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detect_yolo_n8n.py")

BASE_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result")
WINDOWS_HISTORY_PATH = os.path.join(BASE_OUTPUT_DIR, "history_log.json")


# ============================================================
# Raspberry Pi paths
# ============================================================

PI_USER = "your_username"
PI_HOST = "your_pi_ip"
PI_PORT = "2222"
PI_SSH_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "pi_n8n_key")
PI_RESULT_ROOT = "/home/pi/pi_n8n_result"
LOCAL_PI_RESULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_result")
PI_HISTORY_PATH = os.path.join(LOCAL_PI_RESULT_DIR, "history_log.json")


# ============================================================
# Helper: timestamp
# ============================================================

def extract_timestamp_from_paths(summary_data):
    paths = [
        summary_data.get("timestamp"),
        summary_data.get("output_dir"),
        summary_data.get("summary_path"),
        summary_data.get("remote_output_dir"),
        summary_data.get("remote_summary_path")
    ]

    for path in paths:
        if not path:
            continue

        match = re.search(r"(\d{8}_\d{6})", str(path))
        if match:
            return match.group(1)

    return None


def get_record_timestamp(record):
    timestamp = record.get("timestamp")

    if timestamp:
        return str(timestamp)

    extracted = extract_timestamp_from_paths(record)
    return extracted or ""


# ============================================================
# Helper: history path by source
# ============================================================

def get_history_path(source):
    source = source.lower()

    if source == "windows":
        return WINDOWS_HISTORY_PATH

    if source == "pi":
        return PI_HISTORY_PATH

    return None


def get_history_label(source):
    source = source.lower()

    if source == "windows":
        return "Windows"

    if source == "pi":
        return "Raspberry Pi 5"

    if source == "all":
        return "All"

    return "Unknown"


# ============================================================
# Helper: get latest result folder
# ============================================================

def get_latest_result_folder(base_output_dir):
    if not os.path.exists(base_output_dir):
        return None

    result_folders = [
        os.path.join(base_output_dir, folder)
        for folder in os.listdir(base_output_dir)
        if os.path.isdir(os.path.join(base_output_dir, folder))
    ]

    if len(result_folders) == 0:
        return None

    return max(result_folders, key=os.path.getmtime)


def get_latest_local_pi_result_folder():
    return get_latest_result_folder(LOCAL_PI_RESULT_DIR)


# ============================================================
# Helper: get latest summary.json
# ============================================================

def get_latest_summary_path(base_output_dir):
    latest_folder = get_latest_result_folder(base_output_dir)

    if latest_folder is None:
        return None

    summary_path = os.path.join(latest_folder, "summary.json")

    if not os.path.exists(summary_path):
        return None

    return summary_path


# ============================================================
# Helper: load / save history
# ============================================================

def load_history(history_path):
    if not os.path.exists(history_path):
        return []

    try:
        with open(history_path, "r", encoding="utf-8") as f:
            history = json.load(f)

        if not isinstance(history, list):
            return []

        return history

    except json.JSONDecodeError:
        return []


def save_history(history, history_path):
    os.makedirs(os.path.dirname(history_path), exist_ok=True)

    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def load_history_by_source(source):
    source = source.lower()

    if source == "windows":
        return load_history(WINDOWS_HISTORY_PATH)

    if source == "pi":
        return load_history(PI_HISTORY_PATH)

    if source == "all":
        windows_history = load_history(WINDOWS_HISTORY_PATH)
        pi_history = load_history(PI_HISTORY_PATH)
        combined = windows_history + pi_history

        combined.sort(
            key=lambda x: get_record_timestamp(x),
            reverse=False
        )

        return combined

    return None


# ============================================================
# Helper: add file existence status
# ============================================================

def add_record_status(record):
    record = dict(record)

    summary_path = record.get("summary_path", "")
    output_dir = record.get("output_dir", "")

    record["summary_exists"] = bool(summary_path and os.path.exists(summary_path))
    record["output_exists"] = bool(output_dir and os.path.exists(output_dir))

    if not record.get("timestamp"):
        record["timestamp"] = extract_timestamp_from_paths(record)

    return record


# ============================================================
# Helper: append history
# ============================================================

def append_history(summary_data, history_path):
    timestamp = summary_data.get("timestamp") or extract_timestamp_from_paths(summary_data)

    history_item = {
        "status": summary_data.get("status"),
        "task": summary_data.get("task"),
        "device": summary_data.get("device"),
        "source": summary_data.get("source"),
        "timestamp": timestamp,
        "model": summary_data.get("model"),
        "confidence_threshold": summary_data.get("confidence_threshold"),
        "total_images": summary_data.get("total_images"),
        "detected_images": summary_data.get("detected_images"),
        "total_boxes": summary_data.get("total_boxes"),
        "avg_confidence": summary_data.get("avg_confidence"),
        "output_dir": summary_data.get("output_dir"),
        "summary_path": summary_data.get("summary_path"),
        "execution_time_sec": summary_data.get("execution_time_sec"),
        "flask_execution_time_sec": summary_data.get("flask_execution_time_sec"),
        "remote_output_dir": summary_data.get("remote_output_dir"),
        "remote_summary_path": summary_data.get("remote_summary_path")
    }

    history = load_history(history_path)
    history.append(history_item)
    save_history(history, history_path)

    return history_item


# ============================================================
# Helper: copy latest Raspberry Pi result to Windows
# ============================================================

def copy_latest_pi_result(open_after_copy=False):
    os.makedirs(LOCAL_PI_RESULT_DIR, exist_ok=True)

    ssh_cmd = [
        "ssh",
        "-i", PI_SSH_KEY,
        "-p", PI_PORT,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{PI_USER}@{PI_HOST}",
        f"ls -td {PI_RESULT_ROOT}/* | head -1"
    ]

    latest_remote_dir = subprocess.check_output(
        ssh_cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30
    ).strip()

    if not latest_remote_dir:
        return {
            "status": "error",
            "source": "pi",
            "message": "No Raspberry Pi result folder found.",
            "remote_root": PI_RESULT_ROOT
        }

    folder_name = os.path.basename(latest_remote_dir.rstrip("/"))
    local_target_dir = os.path.join(LOCAL_PI_RESULT_DIR, folder_name)

    if os.path.exists(local_target_dir):
        shutil.rmtree(local_target_dir)

    scp_cmd = [
        "scp",
        "-i", PI_SSH_KEY,
        "-P", PI_PORT,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-r",
        f"{PI_USER}@{PI_HOST}:{latest_remote_dir}",
        LOCAL_PI_RESULT_DIR
    ]

    subprocess.run(
        scp_cmd,
        check=True,
        timeout=120
    )

    local_summary_path = os.path.join(local_target_dir, "summary.json")
    history_saved = False
    history_item = None
    summary = None

    if os.path.exists(local_summary_path):
        with open(local_summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)

        summary["device"] = "Raspberry Pi 5"
        summary["source"] = "pi"
        summary["remote_output_dir"] = summary.get("output_dir", latest_remote_dir)
        summary["remote_summary_path"] = summary.get(
            "summary_path",
            os.path.join(latest_remote_dir, "summary.json").replace("\\", "/")
        )
        summary["output_dir"] = local_target_dir
        summary["summary_path"] = local_summary_path
        summary["flask_execution_time_sec"] = None

        if not summary.get("timestamp"):
            summary["timestamp"] = extract_timestamp_from_paths(summary)

        history_item = append_history(summary, PI_HISTORY_PATH)
        history_saved = True

    if open_after_copy:
        if os.path.exists(local_summary_path):
            subprocess.Popen([
                "explorer",
                "/select,",
                local_summary_path
            ])
        else:
            subprocess.Popen([
                "explorer",
                local_target_dir
            ])

    response = {
        "status": "success",
        "source": "pi",
        "message": "Latest Raspberry Pi result folder copied and Pi history updated.",
        "remote_dir": latest_remote_dir,
        "local_dir": local_target_dir,
        "local_summary_path": local_summary_path,
        "history_saved": history_saved,
        "history_source": "pi",
        "history_path": PI_HISTORY_PATH,
        "opened": bool(open_after_copy)
    }

    if summary is not None:
        response.update(summary)
        response["remote_dir"] = latest_remote_dir
        response["local_dir"] = local_target_dir
        response["local_summary_path"] = local_summary_path
        response["history_saved"] = history_saved
        response["history_source"] = "pi"
        response["history_path"] = PI_HISTORY_PATH
        response["opened"] = bool(open_after_copy)

    if history_item is not None:
        response["history_item"] = add_record_status(history_item)

    if not history_saved:
        response["warning"] = "summary.json was not found in the copied Raspberry Pi result folder."

    return response


# ============================================================
# YOLO Detection API: Windows local detection
# ============================================================

@app.route("/detect", methods=["POST"])
def detect():
    data = request.get_json(silent=True) or {}
    selected_model = data.get("model", "GAM")

    print("\n==============================")
    print("Selected Model:", selected_model)
    print("==============================")

    start_time = time.time()

    result = subprocess.run(
        [PYTHON_EXE, SCRIPT_PATH, selected_model],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    elapsed_time = round(time.time() - start_time, 2)

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    print(stdout)

    if result.returncode != 0:
        return jsonify({
            "status": "error",
            "task": "YOLO-AMC crack detection",
            "source": "windows",
            "message": "YOLO inference failed.",
            "model": selected_model,
            "returncode": result.returncode,
            "execution_time_sec": elapsed_time,
            "error": stderr[-1500:],
            "stdout": stdout[-1500:]
        })

    summary_path = get_latest_summary_path(BASE_OUTPUT_DIR)

    if summary_path is None:
        return jsonify({
            "status": "error",
            "task": "YOLO-AMC crack detection",
            "source": "windows",
            "message": "summary.json not found.",
            "model": selected_model,
            "base_output_dir": BASE_OUTPUT_DIR,
            "execution_time_sec": elapsed_time,
            "stdout": stdout[-1500:]
        })

    with open(summary_path, "r", encoding="utf-8") as f:
        summary = json.load(f)

    summary["device"] = "Windows"
    summary["source"] = "windows"
    summary["flask_execution_time_sec"] = elapsed_time
    summary["summary_path"] = summary_path

    if not summary.get("timestamp"):
        summary["timestamp"] = extract_timestamp_from_paths(summary)

    history_item = append_history(summary, WINDOWS_HISTORY_PATH)

    summary["history_saved"] = True
    summary["history_source"] = "windows"
    summary["history_path"] = WINDOWS_HISTORY_PATH
    summary["history_item"] = add_record_status(history_item)

    return jsonify(summary)


# ============================================================
# Raspberry Pi Sync API
# n8n usage:
# Pi Execution Service -> Pi Sync Service -> Pi Formatter -> Reply
# ============================================================

@app.route("/pi_sync", methods=["GET", "POST"])
def pi_sync():
    open_after_copy = request.args.get("open", "false").lower() in ["1", "true", "yes"]

    try:
        response = copy_latest_pi_result(open_after_copy=open_after_copy)
        return jsonify(response)

    except subprocess.TimeoutExpired:
        return jsonify({
            "status": "error",
            "source": "pi",
            "message": "SSH or SCP command timed out."
        })

    except subprocess.CalledProcessError as e:
        return jsonify({
            "status": "error",
            "source": "pi",
            "message": "SSH or SCP command failed.",
            "error": str(e)
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "source": "pi",
            "message": "Failed to sync Raspberry Pi result folder.",
            "error": str(e)
        })


# ============================================================
# Open Result Folder API
# ============================================================

@app.route("/open_folder", methods=["GET"])
def open_folder():
    source = request.args.get("source", "windows").lower()

    if source == "windows":
        latest_folder = get_latest_result_folder(BASE_OUTPUT_DIR)

        if latest_folder is None:
            return jsonify({
                "status": "error",
                "source": "windows",
                "message": "No Windows result folders found.",
                "output_dir": BASE_OUTPUT_DIR
            })

        os.startfile(latest_folder)

        return jsonify({
            "status": "success",
            "source": "windows",
            "message": "Latest Windows result folder opened.",
            "output_dir": latest_folder
        })

    if source == "pi":
        latest_folder = get_latest_local_pi_result_folder()

        if latest_folder is None:
            return jsonify({
                "status": "error",
                "source": "pi",
                "message": "No local Raspberry Pi result folders found. Please run /pi_sync first.",
                "output_dir": LOCAL_PI_RESULT_DIR
            })

        summary_path = os.path.join(latest_folder, "summary.json")

        if os.path.exists(summary_path):
            subprocess.Popen([
                "explorer",
                "/select,",
                summary_path
            ])
        else:
            os.startfile(latest_folder)

        return jsonify({
            "status": "success",
            "source": "pi",
            "message": "Latest local Raspberry Pi result folder opened.",
            "output_dir": latest_folder,
            "summary_path": summary_path if os.path.exists(summary_path) else None
        })

    return jsonify({
        "status": "error",
        "message": "Invalid source. Use source=windows or source=pi.",
        "source": source
    })



# ============================================================
# History Analysis Helpers
# Used by /history/analyze
# ============================================================

ALLOWED_HISTORY_TYPES = {
    "latest",
    "recent",
    "stats",
    "best_confidence",
    "fastest",
    "clean",
    "clean_history",
    "model_summary",
    "compare_models",
    "device_compare",
    "trend"
}

ALLOWED_METRICS = {
    "avg_confidence",
    "execution_time_sec",
    "flask_execution_time_sec",
    "total_boxes",
    "total_images",
    "detected_images"
}


def parse_positive_int(value, default=5):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default

    if value <= 0:
        return default

    return value


def is_valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def round_or_none(value, digits=3):
    if value is None:
        return None

    if not is_valid_number(value):
        return None

    return round(value, digits)


def normalize_query_type(query_type):
    query_type = str(query_type or "latest").lower().strip()

    alias_map = {
        "best-confidence": "best_confidence",
        "bestconfidence": "best_confidence",
        "fast": "fastest",
        "summary": "model_summary",
        "model-summary": "model_summary",
        "compare-models": "compare_models",
        "compare": "compare_models",
        "device-compare": "device_compare",
        "devicecompare": "device_compare",
        "clean-history": "clean"
    }

    return alias_map.get(query_type, query_type)


def normalize_source(source):
    source = str(source or "all").lower().strip()

    if source in ["win", "window", "windows", "pc"]:
        return "windows"

    if source in ["raspberry", "raspberrypi", "rpi", "pi", "raspberry_pi"]:
        return "pi"

    if source in ["all", "both", "全部"]:
        return "all"

    return source


def normalize_model(model):
    if model is None:
        return ""

    model = str(model).upper().strip()

    if model in ["", "ALL", "NONE", "NULL", "ANY"]:
        return ""

    return model


def normalize_metric(metric):
    metric = str(metric or "avg_confidence").lower().strip()

    alias_map = {
        "confidence": "avg_confidence",
        "avg_conf": "avg_confidence",
        "average_confidence": "avg_confidence",
        "time": "execution_time_sec",
        "execution_time": "execution_time_sec",
        "speed": "execution_time_sec",
        "boxes": "total_boxes",
        "box": "total_boxes",
        "cracks": "total_boxes",
        "crack_count": "total_boxes",
        "images": "total_images",
        "detected": "detected_images"
    }

    return alias_map.get(metric, metric)


def filter_history_records(history, model=None):
    model = normalize_model(model)

    records = list(history)

    if model:
        records = [
            record for record in records
            if normalize_model(record.get("model")) == model
        ]

    records.sort(
        key=lambda x: get_record_timestamp(x),
        reverse=False
    )

    return records


def limit_recent_records(records, limit):
    if limit <= 0:
        limit = 5

    return records[-limit:]


def get_numeric_values(records, field):
    values = []

    for record in records:
        value = record.get(field)

        if is_valid_number(value):
            values.append(value)

    return values


def average_field(records, field, digits=3):
    values = get_numeric_values(records, field)

    if not values:
        return None

    return round(sum(values) / len(values), digits)


def sum_field(records, field):
    total = 0

    for record in records:
        value = record.get(field)

        if is_valid_number(value):
            total += value

    return total


def build_basic_stats(records):
    return {
        "total_runs": len(records),
        "total_images": sum_field(records, "total_images"),
        "total_detected_images": sum_field(records, "detected_images"),
        "total_boxes": sum_field(records, "total_boxes"),
        "avg_confidence": average_field(records, "avg_confidence"),
        "avg_execution_time_sec": average_field(records, "execution_time_sec"),
        "avg_flask_execution_time_sec": average_field(records, "flask_execution_time_sec"),
        "avg_total_boxes": average_field(records, "total_boxes"),
        "avg_detected_images": average_field(records, "detected_images")
    }


def summarize_records(records, source="all", model=""):
    records_with_status = [add_record_status(record) for record in records]
    stats = build_basic_stats(records)

    return {
        "status": "success",
        "source": source,
        "model": model or "all",
        "count": len(records),
        **stats,
        "records": records_with_status
    }


def build_model_summary(source, model, limit):
    history = load_history_by_source(source)

    if history is None:
        return {
            "status": "error",
            "query_type": "model_summary",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        }

    model = normalize_model(model)

    if not model:
        return {
            "status": "error",
            "query_type": "model_summary",
            "message": "model is required for model_summary. Example: model=GAM or model=SA.",
            "source": source
        }

    records = filter_history_records(history, model=model)
    records = limit_recent_records(records, limit)

    if not records:
        return {
            "status": "empty",
            "query_type": "model_summary",
            "source": source,
            "model": model,
            "records": [],
            "message": f"No {get_history_label(source)} history found for model {model}."
        }

    result = summarize_records(records, source=source, model=model)
    result["query_type"] = "model_summary"
    result["message"] = f"Recent {model} performance summary generated successfully."

    return result


def build_compare_models(source, limit):
    history = load_history_by_source(source)

    if history is None:
        return {
            "status": "error",
            "query_type": "compare_models",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        }

    history = filter_history_records(history)
    history = limit_recent_records(history, limit)

    if not history:
        return {
            "status": "empty",
            "query_type": "compare_models",
            "source": source,
            "models": {},
            "message": f"No {get_history_label(source)} detection history found."
        }

    grouped = {}

    for record in history:
        model = normalize_model(record.get("model")) or "UNKNOWN"
        grouped.setdefault(model, []).append(record)

    model_summary = {}

    for model, records in grouped.items():
        model_summary[model] = build_basic_stats(records)

    fastest_model = None
    best_confidence_model = None

    valid_speed = {
        model: data.get("avg_execution_time_sec")
        for model, data in model_summary.items()
        if is_valid_number(data.get("avg_execution_time_sec"))
    }

    valid_conf = {
        model: data.get("avg_confidence")
        for model, data in model_summary.items()
        if is_valid_number(data.get("avg_confidence"))
    }

    if valid_speed:
        fastest_model = min(valid_speed, key=valid_speed.get)

    if valid_conf:
        best_confidence_model = max(valid_conf, key=valid_conf.get)

    return {
        "status": "success",
        "query_type": "compare_models",
        "source": source,
        "limit": limit,
        "models": model_summary,
        "fastest_model": fastest_model,
        "best_confidence_model": best_confidence_model,
        "message": "Model comparison generated successfully."
    }


def build_device_compare(model, limit):
    model = normalize_model(model)

    windows_history = filter_history_records(load_history(WINDOWS_HISTORY_PATH), model=model)
    pi_history = filter_history_records(load_history(PI_HISTORY_PATH), model=model)

    windows_records = limit_recent_records(windows_history, limit)
    pi_records = limit_recent_records(pi_history, limit)

    windows_summary = build_basic_stats(windows_records) if windows_records else None
    pi_summary = build_basic_stats(pi_records) if pi_records else None

    faster_device = None
    better_confidence_device = None

    speed_candidates = {}

    if windows_summary and is_valid_number(windows_summary.get("avg_execution_time_sec")):
        speed_candidates["windows"] = windows_summary.get("avg_execution_time_sec")

    if pi_summary and is_valid_number(pi_summary.get("avg_execution_time_sec")):
        speed_candidates["pi"] = pi_summary.get("avg_execution_time_sec")

    if speed_candidates:
        faster_device = min(speed_candidates, key=speed_candidates.get)

    confidence_candidates = {}

    if windows_summary and is_valid_number(windows_summary.get("avg_confidence")):
        confidence_candidates["windows"] = windows_summary.get("avg_confidence")

    if pi_summary and is_valid_number(pi_summary.get("avg_confidence")):
        confidence_candidates["pi"] = pi_summary.get("avg_confidence")

    if confidence_candidates:
        better_confidence_device = max(confidence_candidates, key=confidence_candidates.get)

    if not windows_records and not pi_records:
        return {
            "status": "empty",
            "query_type": "device_compare",
            "source": "all",
            "model": model or "all",
            "message": "No Windows or Raspberry Pi history found for device comparison.",
            "windows": None,
            "pi": None
        }

    return {
        "status": "success",
        "query_type": "device_compare",
        "source": "all",
        "model": model or "all",
        "limit": limit,
        "windows": {
            "count": len(windows_records),
            **windows_summary
        } if windows_summary else {
            "count": 0
        },
        "pi": {
            "count": len(pi_records),
            **pi_summary
        } if pi_summary else {
            "count": 0
        },
        "faster_device": faster_device,
        "better_confidence_device": better_confidence_device,
        "message": "Device comparison generated successfully."
    }


def build_trend(source, model, metric, limit):
    history = load_history_by_source(source)

    if history is None:
        return {
            "status": "error",
            "query_type": "trend",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        }

    metric = normalize_metric(metric)

    if metric not in ALLOWED_METRICS:
        return {
            "status": "error",
            "query_type": "trend",
            "source": source,
            "metric": metric,
            "message": f"Invalid metric. Use one of: {', '.join(sorted(ALLOWED_METRICS))}."
        }

    model = normalize_model(model)
    records = filter_history_records(history, model=model)
    records = limit_recent_records(records, limit)

    trend_points = []

    for record in records:
        value = record.get(metric)

        if is_valid_number(value):
            trend_points.append({
                "timestamp": get_record_timestamp(record),
                "source": record.get("source"),
                "device": record.get("device"),
                "model": record.get("model"),
                "value": value,
                "record": add_record_status(record)
            })

    if len(trend_points) == 0:
        return {
            "status": "empty",
            "query_type": "trend",
            "source": source,
            "model": model or "all",
            "metric": metric,
            "records": [],
            "trend_points": [],
            "message": "No valid numeric values found for trend analysis."
        }

    first_value = trend_points[0]["value"]
    last_value = trend_points[-1]["value"]
    delta = round(last_value - first_value, 3)

    if abs(delta) < 0.001:
        direction = "stable"
    elif delta > 0:
        direction = "increasing"
    else:
        direction = "decreasing"

    if len(trend_points) >= 2 and first_value != 0:
        change_percent = round((delta / first_value) * 100, 2)
    else:
        change_percent = None

    values = [point["value"] for point in trend_points]

    return {
        "status": "success",
        "query_type": "trend",
        "source": source,
        "model": model or "all",
        "metric": metric,
        "limit": limit,
        "count": len(trend_points),
        "first_value": first_value,
        "last_value": last_value,
        "delta": delta,
        "change_percent": change_percent,
        "direction": direction,
        "min_value": min(values),
        "max_value": max(values),
        "avg_value": round(sum(values) / len(values), 3),
        "trend_points": trend_points,
        "message": "Trend analysis generated successfully."
    }


# ============================================================
# History API: unified analyzer
# n8n usage:
# /history/analyze?type=trend&source=pi&model=GAM&metric=avg_confidence&limit=5
# ============================================================

@app.route("/history/analyze", methods=["GET"])
def history_analyze():
    query_type = normalize_query_type(request.args.get("type", "latest"))
    source = normalize_source(request.args.get("source", "all"))
    model = normalize_model(request.args.get("model", ""))
    metric = normalize_metric(request.args.get("metric", "avg_confidence"))
    limit = parse_positive_int(request.args.get("limit", 5), default=5)

    if query_type not in ALLOWED_HISTORY_TYPES:
        return jsonify({
            "status": "error",
            "query_type": query_type,
            "message": (
                "Invalid history analysis type. Use one of: "
                + ", ".join(sorted(ALLOWED_HISTORY_TYPES))
            )
        })

    # Keep backward-compatible query types inside the unified endpoint.
    if query_type == "latest":
        history = load_history_by_source(source)

        if history is None:
            return jsonify({
                "status": "error",
                "query_type": "latest",
                "message": "Invalid source. Use source=windows, source=pi, or source=all.",
                "source": source
            })

        records = filter_history_records(history, model=model)

        if not records:
            return jsonify({
                "status": "empty",
                "query_type": "latest",
                "source": source,
                "model": model or "all",
                "records": [],
                "message": f"No {get_history_label(source)} detection history found."
            })

        return jsonify({
            "status": "success",
            "query_type": "latest",
            "source": source,
            "model": model or "all",
            "records": [add_record_status(records[-1])]
        })

    if query_type == "recent":
        history = load_history_by_source(source)

        if history is None:
            return jsonify({
                "status": "error",
                "query_type": "recent",
                "message": "Invalid source. Use source=windows, source=pi, or source=all.",
                "source": source
            })

        records = filter_history_records(history, model=model)
        records = limit_recent_records(records, limit)
        records = [add_record_status(record) for record in records]

        return jsonify({
            "status": "success",
            "query_type": "recent",
            "source": source,
            "model": model or "all",
            "limit": limit,
            "count": len(records),
            "records": records
        })

    if query_type == "stats":
        history = load_history_by_source(source)

        if history is None:
            return jsonify({
                "status": "error",
                "query_type": "stats",
                "message": "Invalid source. Use source=windows, source=pi, or source=all.",
                "source": source
            })

        records = filter_history_records(history, model=model)

        if not records:
            return jsonify({
                "status": "empty",
                "query_type": "stats",
                "source": source,
                "model": model or "all",
                "records": [],
                "message": f"No {get_history_label(source)} detection history found."
            })

        result = summarize_records(records, source=source, model=model)
        result["query_type"] = "stats"

        model_count = {}
        device_count = {}

        for record in records:
            record_model = record.get("model", "unknown")
            device = record.get("device", "unknown")

            model_count[record_model] = model_count.get(record_model, 0) + 1
            device_count[device] = device_count.get(device, 0) + 1

        result["model_count"] = model_count
        result["device_count"] = device_count

        return jsonify(result)

    if query_type == "best_confidence":
        history = load_history_by_source(source)

        if history is None:
            return jsonify({
                "status": "error",
                "query_type": "best_confidence",
                "message": "Invalid source. Use source=windows, source=pi, or source=all.",
                "source": source
            })

        records = filter_history_records(history, model=model)
        valid_records = [
            record for record in records
            if is_valid_number(record.get("avg_confidence"))
        ]

        if not valid_records:
            return jsonify({
                "status": "empty",
                "query_type": "best_confidence",
                "source": source,
                "model": model or "all",
                "records": [],
                "message": "No valid average confidence value found in history."
            })

        best = max(valid_records, key=lambda x: x.get("avg_confidence", 0))

        return jsonify({
            "status": "success",
            "query_type": "best_confidence",
            "source": source,
            "model": model or "all",
            "records": [add_record_status(best)]
        })

    if query_type == "fastest":
        history = load_history_by_source(source)

        if history is None:
            return jsonify({
                "status": "error",
                "query_type": "fastest",
                "message": "Invalid source. Use source=windows, source=pi, or source=all.",
                "source": source
            })

        records = filter_history_records(history, model=model)
        valid_records = [
            record for record in records
            if is_valid_number(record.get("execution_time_sec"))
        ]

        if not valid_records:
            return jsonify({
                "status": "empty",
                "query_type": "fastest",
                "source": source,
                "model": model or "all",
                "records": [],
                "message": "No valid execution time found in history."
            })

        fastest = min(valid_records, key=lambda x: x.get("execution_time_sec", float("inf")))

        return jsonify({
            "status": "success",
            "query_type": "fastest",
            "source": source,
            "model": model or "all",
            "records": [add_record_status(fastest)]
        })

    if query_type in ["clean", "clean_history"]:
        # clean_single_history is defined below in the original code.
        if source == "windows":
            result = clean_single_history(WINDOWS_HISTORY_PATH)

            return jsonify({
                "status": "success",
                "query_type": "clean_history",
                "source": "windows",
                "message": "Windows history log cleaned successfully.",
                **result
            })

        if source == "pi":
            result = clean_single_history(PI_HISTORY_PATH)

            return jsonify({
                "status": "success",
                "query_type": "clean_history",
                "source": "pi",
                "message": "Raspberry Pi history log cleaned successfully.",
                **result
            })

        if source == "all":
            windows_result = clean_single_history(WINDOWS_HISTORY_PATH)
            pi_result = clean_single_history(PI_HISTORY_PATH)

            return jsonify({
                "status": "success",
                "query_type": "clean_history",
                "source": "all",
                "message": "Windows and Raspberry Pi history logs cleaned successfully.",
                "windows": windows_result,
                "pi": pi_result,
                "before_count": windows_result["before_count"] + pi_result["before_count"],
                "after_count": windows_result["after_count"] + pi_result["after_count"],
                "removed_count": windows_result["removed_count"] + pi_result["removed_count"]
            })

        return jsonify({
            "status": "error",
            "query_type": "clean_history",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        })

    if query_type == "model_summary":
        return jsonify(build_model_summary(source=source, model=model, limit=limit))

    if query_type == "compare_models":
        return jsonify(build_compare_models(source=source, limit=limit))

    if query_type == "device_compare":
        return jsonify(build_device_compare(model=model, limit=limit))

    if query_type == "trend":
        return jsonify(build_trend(
            source=source,
            model=model,
            metric=metric,
            limit=limit
        ))

    return jsonify({
        "status": "error",
        "query_type": query_type,
        "message": "Unhandled history analysis type."
    })


# ============================================================
# History API: latest
# ============================================================

@app.route("/history/latest", methods=["GET"])
def history_latest():
    source = request.args.get("source", "all").lower()
    history = load_history_by_source(source)

    if history is None:
        return jsonify({
            "status": "error",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        })

    if not history:
        return jsonify({
            "status": "empty",
            "query_type": "latest",
            "source": source,
            "records": [],
            "message": f"No {get_history_label(source)} detection history found."
        })

    latest = add_record_status(history[-1])

    return jsonify({
        "status": "success",
        "query_type": "latest",
        "source": source,
        "records": [latest]
    })


# ============================================================
# History API: recent
# ============================================================

@app.route("/history/recent", methods=["GET"])
def history_recent():
    source = request.args.get("source", "all").lower()
    history = load_history_by_source(source)

    if history is None:
        return jsonify({
            "status": "error",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        })

    limit = request.args.get("limit", 5)

    try:
        limit = int(limit)
    except ValueError:
        limit = 5

    if limit <= 0:
        limit = 5

    records = history[-limit:]
    records = [add_record_status(record) for record in records]

    return jsonify({
        "status": "success",
        "query_type": "recent",
        "source": source,
        "count": len(records),
        "records": records
    })


# ============================================================
# History API: best confidence
# ============================================================

@app.route("/history/best-confidence", methods=["GET"])
def history_best_confidence():
    source = request.args.get("source", "all").lower()
    history = load_history_by_source(source)

    if history is None:
        return jsonify({
            "status": "error",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        })

    if not history:
        return jsonify({
            "status": "empty",
            "query_type": "best_confidence",
            "source": source,
            "records": [],
            "message": f"No {get_history_label(source)} detection history found."
        })

    valid_records = [
        record for record in history
        if isinstance(record.get("avg_confidence"), (int, float))
    ]

    if not valid_records:
        return jsonify({
            "status": "empty",
            "query_type": "best_confidence",
            "source": source,
            "records": [],
            "message": "No valid average confidence value found in history."
        })

    best = max(valid_records, key=lambda x: x.get("avg_confidence", 0))
    best = add_record_status(best)

    return jsonify({
        "status": "success",
        "query_type": "best_confidence",
        "source": source,
        "records": [best]
    })


# ============================================================
# History API: fastest
# ============================================================

@app.route("/history/fastest", methods=["GET"])
def history_fastest():
    source = request.args.get("source", "all").lower()
    history = load_history_by_source(source)

    if history is None:
        return jsonify({
            "status": "error",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        })

    if not history:
        return jsonify({
            "status": "empty",
            "query_type": "fastest",
            "source": source,
            "records": [],
            "message": f"No {get_history_label(source)} detection history found."
        })

    valid_records = [
        record for record in history
        if isinstance(record.get("execution_time_sec"), (int, float))
    ]

    if not valid_records:
        return jsonify({
            "status": "empty",
            "query_type": "fastest",
            "source": source,
            "records": [],
            "message": "No valid execution time found in history."
        })

    fastest = min(valid_records, key=lambda x: x.get("execution_time_sec", float("inf")))
    fastest = add_record_status(fastest)

    return jsonify({
        "status": "success",
        "query_type": "fastest",
        "source": source,
        "records": [fastest]
    })


# ============================================================
# History API: stats
# ============================================================

@app.route("/history/stats", methods=["GET"])
def history_stats():
    source = request.args.get("source", "all").lower()
    history = load_history_by_source(source)

    if history is None:
        return jsonify({
            "status": "error",
            "message": "Invalid source. Use source=windows, source=pi, or source=all.",
            "source": source
        })

    if not history:
        return jsonify({
            "status": "empty",
            "query_type": "stats",
            "source": source,
            "records": [],
            "message": f"No {get_history_label(source)} detection history found."
        })

    total_runs = len(history)
    total_images = sum(record.get("total_images", 0) or 0 for record in history)
    total_detected_images = sum(record.get("detected_images", 0) or 0 for record in history)
    total_boxes = sum(record.get("total_boxes", 0) or 0 for record in history)

    avg_conf_list = [
        record.get("avg_confidence")
        for record in history
        if isinstance(record.get("avg_confidence"), (int, float))
    ]

    execution_time_list = [
        record.get("execution_time_sec")
        for record in history
        if isinstance(record.get("execution_time_sec"), (int, float))
    ]

    avg_confidence_overall = (
        round(sum(avg_conf_list) / len(avg_conf_list), 3)
        if avg_conf_list else None
    )

    avg_execution_time_sec = (
        round(sum(execution_time_list) / len(execution_time_list), 3)
        if execution_time_list else None
    )

    model_count = {}
    device_count = {}

    for record in history:
        model = record.get("model", "unknown")
        device = record.get("device", "unknown")

        model_count[model] = model_count.get(model, 0) + 1
        device_count[device] = device_count.get(device, 0) + 1

    records = [add_record_status(record) for record in history]

    return jsonify({
        "status": "success",
        "query_type": "stats",
        "source": source,
        "total_runs": total_runs,
        "total_images": total_images,
        "total_detected_images": total_detected_images,
        "total_boxes": total_boxes,
        "avg_confidence_overall": avg_confidence_overall,
        "avg_execution_time_sec": avg_execution_time_sec,
        "model_count": model_count,
        "device_count": device_count,
        "records": records
    })


# ============================================================
# History API: clean missing records
# ============================================================

def clean_single_history(history_path):
    history = load_history(history_path)

    if not history:
        return {
            "history_path": history_path,
            "before_count": 0,
            "after_count": 0,
            "removed_count": 0,
            "removed_records": []
        }

    valid_records = []
    removed_records = []

    for record in history:
        summary_path = record.get("summary_path", "")
        output_dir = record.get("output_dir", "")

        summary_exists = bool(summary_path and os.path.exists(summary_path))
        output_exists = bool(output_dir and os.path.exists(output_dir))

        if summary_exists and output_exists:
            valid_records.append(record)
        else:
            removed_record = dict(record)
            removed_record["summary_exists"] = summary_exists
            removed_record["output_exists"] = output_exists
            removed_records.append(removed_record)

    save_history(valid_records, history_path)

    return {
        "history_path": history_path,
        "before_count": len(history),
        "after_count": len(valid_records),
        "removed_count": len(removed_records),
        "removed_records": removed_records
    }


@app.route("/history/clean", methods=["GET"])
def history_clean():
    source = request.args.get("source", "all").lower()

    if source == "windows":
        result = clean_single_history(WINDOWS_HISTORY_PATH)

        return jsonify({
            "status": "success",
            "query_type": "clean_history",
            "source": "windows",
            "message": "Windows history log cleaned successfully.",
            **result
        })

    if source == "pi":
        result = clean_single_history(PI_HISTORY_PATH)

        return jsonify({
            "status": "success",
            "query_type": "clean_history",
            "source": "pi",
            "message": "Raspberry Pi history log cleaned successfully.",
            **result
        })

    if source == "all":
        windows_result = clean_single_history(WINDOWS_HISTORY_PATH)
        pi_result = clean_single_history(PI_HISTORY_PATH)

        return jsonify({
            "status": "success",
            "query_type": "clean_history",
            "source": "all",
            "message": "Windows and Raspberry Pi history logs cleaned successfully.",
            "windows": windows_result,
            "pi": pi_result,
            "before_count": windows_result["before_count"] + pi_result["before_count"],
            "after_count": windows_result["after_count"] + pi_result["after_count"],
            "removed_count": windows_result["removed_count"] + pi_result["removed_count"]
        })

    return jsonify({
        "status": "error",
        "message": "Invalid source. Use source=windows, source=pi, or source=all.",
        "source": source
    })


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("======================================")
    print("YOLO-AMC Flask API Running")
    print("http://0.0.0.0:5000")
    print("======================================")
    print("Windows result folder:", BASE_OUTPUT_DIR)
    print("Windows history log:", WINDOWS_HISTORY_PATH)
    print("======================================")
    print("Pi result folder:", LOCAL_PI_RESULT_DIR)
    print("Pi history log:", PI_HISTORY_PATH)
    print("======================================")
    print("Pi sync API: http://0.0.0.0:5000/pi_sync")
    print("History analyze API: http://0.0.0.0:5000/history/analyze")
    print("======================================")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
