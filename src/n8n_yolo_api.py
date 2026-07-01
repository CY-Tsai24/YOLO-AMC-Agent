import subprocess
import time
import json
import os
import shutil
import re
import shlex
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)
print("========== THIS IS NEW API ==========")

# ============================================================
# Global paths
# ============================================================

PYTHON_EXE = r"YOUR_PYTHON_PATH"
SCRIPT_PATH = r"PATH_TO_detect_yolo_n8n.py"



BASE_DIR = Path(__file__).resolve().parent

BASE_OUTPUT_DIR = BASE_DIR / "result"
WINDOWS_HISTORY_PATH = BASE_OUTPUT_DIR / "history_log.json"


# ============================================================
# Raspberry Pi paths
# ============================================================

PI_USER = "YOUR_PI_USERNAME"
PI_HOST = "YOUR_PI_HOST"
PI_PORT = "2222"
PI_SSH_KEY = r"PATH_TO_YOUR_SSH_KEY"

PI_RESULT_ROOT = "/home/YOUR_PI_USERNAME/pi_n8n_result"

LOCAL_PI_RESULT_DIR = BASE_DIR / "pi_result"
PI_HISTORY_PATH = LOCAL_PI_RESULT_DIR / "history_log.json"

# ============================================================
# Raspberry Pi Runtime Configuration
# Modify according to your Raspberry Pi environment.
# ============================================================

PI_REMOTE_WORKDIR = "/path/to/your/working_directory"
PI_REMOTE_PYTHON = "/path/to/your/python3"
PI_DETECT_SCRIPT = "/path/to/your/detection_script.py"
PI_REMOTE_INPUT_ROOT = "/path/to/your/input_folder"
PI_RESULT_ROOT = "/path/to/your/output_folder"

PI_DETECT_COMMAND_TEMPLATE = (
    "cd {workdir} && "
    "{python} {script} "
    "--input_dir {input_dir} "
    "--model {model} "
    "--output_root {output_root}"
)


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
# Helper: result images for Streamlit UI
# ============================================================

def get_result_images(output_dir):
    if not output_dir or not os.path.exists(output_dir):
        return []

    valid_exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

    image_files = []

    for root, _, files in os.walk(output_dir):
        for filename in files:
            if filename.lower().endswith(valid_exts):
                image_files.append(os.path.join(root, filename))

    image_files.sort(key=os.path.getmtime, reverse=True)

    return image_files


def enrich_with_result_images(data):
    data = dict(data)

    output_dir = data.get("output_dir") or data.get("local_dir")

    result_images = get_result_images(output_dir)

    data["result_images"] = result_images
    data["latest_result_image"] = result_images[0] if result_images else None

    return data


# ============================================================
# Helper: Raspberry Pi SSH / SCP utilities
# ============================================================

def ssh_pi(command, timeout=120):
    ssh_cmd = [
        "ssh",
        "-i", PI_SSH_KEY,
        "-p", PI_PORT,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{PI_USER}@{PI_HOST}",
        command
    ]

    return subprocess.run(
        ssh_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout
    )


def scp_file_to_pi(local_path, remote_dir, timeout=120):
    os_path = os.path.abspath(local_path)

    scp_cmd = [
        "scp",
        "-i", PI_SSH_KEY,
        "-P", PI_PORT,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        os_path,
        f"{PI_USER}@{PI_HOST}:{remote_dir}/"
    ]

    return subprocess.run(
        scp_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout
    )


def extract_json_from_stdout(stdout):
    if not stdout:
        return None

    text = stdout.strip()

    # Most Pi scripts print pure JSON. Try this first.
    try:
        return json.loads(text)
    except Exception:
        pass

    # Fallback: find the last JSON object in mixed logs.
    matches = re.findall(r"\{[\s\S]*\}", text)

    for candidate in reversed(matches):
        try:
            return json.loads(candidate)
        except Exception:
            continue

    return None


def build_remote_pi_input_dir(local_image_path):
    filename = os.path.basename(local_image_path)
    safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    session_name = f"{timestamp}_{os.path.splitext(safe_filename)[0]}"
    remote_dir = f"{PI_REMOTE_INPUT_ROOT}/{session_name}"
    return remote_dir, safe_filename


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

    response = enrich_with_result_images(response)

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

    summary = enrich_with_result_images(summary)

    return jsonify(summary)



# ============================================================
# Raspberry Pi Detection API: run Pi detection on Streamlit uploaded images
# n8n usage:
# PI Execution Service -> POST http://host.docker.internal:5000/pi_detect
# Body JSON examples:
# Single image:
# {
#   "image_path": "image_save/example.jpg",
#   "model": "GAM"
# }
# Batch images:
# {
#   "image_paths": [
#     "image_save/1.jpg",
#     ""image_save/2.jpg""
#   ],
#   "model": "GAM",
#   "batch_mode": true
# }
# Or folder mode:
# {
#   "image_dir": "C:\\Users\\user\\Desktop\\n8n\\image_save",
#   "model": "GAM",
#   "batch_mode": true
# }
# Then call /pi_sync to copy latest result back to Windows.
# Optional: /pi_detect?sync=true will run detection and sync immediately.
# ============================================================

VALID_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


def normalize_windows_image_paths(data):
    """Return a validated list of local Windows image paths.

    Priority:
    1. image_paths list
    2. image_dir folder
    3. image_path / input_image / path single file
    """

    image_paths = []

    raw_paths = data.get("image_paths")

    if isinstance(raw_paths, list):
        image_paths = raw_paths

    elif isinstance(raw_paths, str) and raw_paths.strip():
        # Accept a single string for compatibility.
        image_paths = [raw_paths]

    elif data.get("image_dir"):
        image_dir = str(data.get("image_dir")).lstrip("=").strip()
        image_dir = os.path.abspath(image_dir)

        if os.path.isdir(image_dir):
            for filename in os.listdir(image_dir):
                full_path = os.path.join(image_dir, filename)
                if (
                    os.path.isfile(full_path)
                    and filename.lower().endswith(VALID_IMAGE_EXTS)
                ):
                    image_paths.append(full_path)

            image_paths.sort()

    else:
        single_path = data.get("image_path") or data.get("input_image") or data.get("path")

        if single_path:
            image_paths = [single_path]

    cleaned_paths = []

    for path in image_paths:
        if path is None:
            continue

        path = str(path).lstrip("=").strip()

        if not path:
            continue

        abs_path = os.path.abspath(path)

        if abs_path.lower().endswith(VALID_IMAGE_EXTS):
            cleaned_paths.append(abs_path)

    # Remove duplicates while preserving order.
    unique_paths = []
    seen = set()

    for path in cleaned_paths:
        key = os.path.normcase(path)
        if key not in seen:
            unique_paths.append(path)
            seen.add(key)

    return unique_paths


def build_remote_pi_input_dir_for_batch(image_paths):
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    if len(image_paths) == 1:
        filename = os.path.basename(image_paths[0])
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
        session_name = f"{timestamp}_{os.path.splitext(safe_name)[0]}"
    else:
        session_name = f"{timestamp}_batch_{len(image_paths)}"

    remote_dir = f"{PI_REMOTE_INPUT_ROOT}/{session_name}"
    return remote_dir


@app.route("/pi_detect", methods=["POST"])
def pi_detect():
    data = request.get_json(silent=True) or {}
    print("PI detect raw data:", data, flush=True)

    selected_model = str(data.get("model", "GAM") or "GAM").upper().strip()
    sync_after = request.args.get("sync", "false").lower() in ["1", "true", "yes"]

    if selected_model not in ["GAM", "SA"]:
        selected_model = "GAM"

    image_paths = normalize_windows_image_paths(data)

    if not image_paths:
        return jsonify({
            "status": "error",
            "source": "pi",
            "message": "image_paths, image_dir, or image_path is required for Raspberry Pi detection.",
            "examples": {
                "single_image": {
                    "image_path": r"C:\Users\user\Desktop\n8n\image_save\example.jpg",
                    "model": "GAM"
                },
                "batch_images": {
                    "image_paths": [
                        r"C:\Users\user\Desktop\n8n\image_save\1.jpg",
                        r"C:\Users\user\Desktop\n8n\image_save\2.jpg"
                    ],
                    "model": "GAM",
                    "batch_mode": True
                },
                "folder_mode": {
                    "image_dir": r"C:\Users\user\Desktop\n8n\image_save",
                    "model": "GAM",
                    "batch_mode": True
                }
            }
        })

    missing_paths = [path for path in image_paths if not os.path.exists(path)]

    if missing_paths:
        return jsonify({
            "status": "error",
            "source": "pi",
            "message": "One or more input images do not exist on Windows.",
            "missing_paths": missing_paths,
            "image_paths": image_paths
        })

    remote_input_dir = build_remote_pi_input_dir_for_batch(image_paths)

    start_time = time.time()

    try:
        # 1. Create a clean batch input folder on Raspberry Pi.
        mkdir_cmd = f"rm -rf {shlex.quote(remote_input_dir)} && mkdir -p {shlex.quote(remote_input_dir)}"
        mkdir_result = ssh_pi(mkdir_cmd, timeout=30)

        if mkdir_result.returncode != 0:
            return jsonify({
                "status": "error",
                "source": "pi",
                "message": "Failed to create Raspberry Pi input folder.",
                "remote_input_dir": remote_input_dir,
                "stderr": mkdir_result.stderr[-1500:],
                "stdout": mkdir_result.stdout[-1500:]
            })

        # 2. Upload all Streamlit images to Raspberry Pi.
        uploaded_remote_images = []
        upload_errors = []

        for image_path in image_paths:
            scp_result = scp_file_to_pi(image_path, remote_input_dir, timeout=120)

            if scp_result.returncode != 0:
                upload_errors.append({
                    "image_path": image_path,
                    "stderr": scp_result.stderr[-1500:],
                    "stdout": scp_result.stdout[-1500:]
                })
            else:
                uploaded_remote_images.append(
                    f"{remote_input_dir}/{os.path.basename(image_path)}"
                )

        if upload_errors:
            return jsonify({
                "status": "error",
                "source": "pi",
                "message": "Failed to upload one or more images to Raspberry Pi.",
                "remote_input_dir": remote_input_dir,
                "image_paths": image_paths,
                "uploaded_remote_images": uploaded_remote_images,
                "upload_errors": upload_errors
            })

        # 3. Execute Raspberry Pi detection on the uploaded folder.
        detect_cmd = PI_DETECT_COMMAND_TEMPLATE.format(
            python=shlex.quote(PI_REMOTE_PYTHON),
            script=shlex.quote(PI_DETECT_SCRIPT),
            input_dir=shlex.quote(remote_input_dir),
            input_image=shlex.quote(uploaded_remote_images[0]) if uploaded_remote_images else "",
            model=shlex.quote(selected_model),
            output_root=shlex.quote(PI_RESULT_ROOT)
        )

        print("PI detect command:", detect_cmd, flush=True)
        detect_result = ssh_pi(detect_cmd, timeout=600)
        elapsed_time = round(time.time() - start_time, 2)

        stdout = detect_result.stdout or ""
        stderr = detect_result.stderr or ""

        print("PI stdout:", stdout, flush=True)
        print("PI stderr:", stderr, flush=True)
        print("PI returncode:", detect_result.returncode, flush=True)

        if detect_result.returncode != 0:
            return jsonify({
                "status": "error",
                "source": "pi",
                "task": "YOLO-AMC Raspberry Pi crack detection",
                "message": "Raspberry Pi detection command failed.",
                "model": selected_model,
                "image_paths": image_paths,
                "total_images": len(image_paths),
                "remote_input_dir": remote_input_dir,
                "uploaded_remote_images": uploaded_remote_images,
                "command": detect_cmd,
                "returncode": detect_result.returncode,
                "flask_execution_time_sec": elapsed_time,
                "stderr": stderr[-3000:],
                "stdout": stdout[-3000:]
            })

        parsed_summary = extract_json_from_stdout(stdout) or {}

        response = {
            "status": "success",
            "source": "pi",
            "device": "Raspberry Pi 5",
            "task": "YOLO-AMC Raspberry Pi crack detection",
            "message": "Raspberry Pi detection completed for uploaded Streamlit images.",
            "model": selected_model,
            "batch_mode": len(image_paths) > 1 or bool(data.get("batch_mode")),
            "image_paths": image_paths,
            "total_images": len(image_paths),
            "remote_input_dir": remote_input_dir,
            "uploaded_remote_images": uploaded_remote_images,
            "flask_execution_time_sec": elapsed_time,
            "stdout": stdout[-3000:],
            "stderr": stderr[-3000:]
        }

        if isinstance(parsed_summary, dict):
            response.update(parsed_summary)
            response["source"] = "pi"
            response["device"] = "Raspberry Pi 5"
            response["model"] = response.get("model") or selected_model
            response["batch_mode"] = len(image_paths) > 1 or bool(data.get("batch_mode"))
            response["image_paths"] = image_paths
            response["total_images"] = response.get("total_images") or len(image_paths)
            response["remote_input_dir"] = remote_input_dir
            response["uploaded_remote_images"] = uploaded_remote_images
            response["flask_execution_time_sec"] = elapsed_time

        # 4. Optional: sync latest Pi result back immediately.
        # If your n8n workflow already has Pi Sync Service after this node,
        # keep sync=false and let /pi_sync run in the next node.
        if sync_after:
            sync_response = copy_latest_pi_result(open_after_copy=False)
            response["sync"] = sync_response

            # If sync returns a local output_dir, make the response directly usable by Streamlit.
            if isinstance(sync_response, dict) and sync_response.get("status") == "success":
                response["local_sync"] = sync_response
                response["local_dir"] = sync_response.get("local_dir")
                response["local_summary_path"] = sync_response.get("local_summary_path")

        response = enrich_with_result_images(response)

        return jsonify(response)

    except subprocess.TimeoutExpired:
        return jsonify({
            "status": "error",
            "source": "pi",
            "message": "Raspberry Pi SSH/SCP/detection command timed out.",
            "image_paths": image_paths,
            "remote_input_dir": remote_input_dir
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "source": "pi",
            "message": "Failed to run Raspberry Pi detection for uploaded images.",
            "image_paths": image_paths,
            "remote_input_dir": remote_input_dir,
            "error": str(e)
        })


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
# Streamlit Helper API: latest result image
# ============================================================

@app.route("/latest_result", methods=["GET"])
def latest_result():
    source = request.args.get("source", "windows").lower()

    if source == "windows":
        latest_folder = get_latest_result_folder(BASE_OUTPUT_DIR)

        if latest_folder is None:
            return jsonify({
                "status": "empty",
                "source": "windows",
                "message": "No Windows result folders found.",
                "output_dir": BASE_OUTPUT_DIR,
                "result_images": [],
                "latest_result_image": None
            })

        response = {
            "status": "success",
            "source": "windows",
            "output_dir": latest_folder
        }

        return jsonify(enrich_with_result_images(response))

    if source == "pi":
        latest_folder = get_latest_local_pi_result_folder()

        if latest_folder is None:
            return jsonify({
                "status": "empty",
                "source": "pi",
                "message": "No local Raspberry Pi result folders found.",
                "output_dir": LOCAL_PI_RESULT_DIR,
                "result_images": [],
                "latest_result_image": None
            })

        response = {
            "status": "success",
            "source": "pi",
            "output_dir": latest_folder
        }

        return jsonify(enrich_with_result_images(response))

    return jsonify({
        "status": "error",
        "message": "Invalid source. Use source=windows or source=pi.",
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
    print("Pi detect API: http://0.0.0.0:5000/pi_detect")
    print("Pi sync API: http://0.0.0.0:5000/pi_sync")
    print("History analyze API: http://0.0.0.0:5000/history/analyze")
    print("Latest result API: http://0.0.0.0:5000/latest_result?source=windows")
    print("======================================")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
