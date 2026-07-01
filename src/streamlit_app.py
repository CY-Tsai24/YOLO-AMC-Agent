import streamlit as st
import requests
import shutil
from pathlib import Path
from PIL import Image
from io import BytesIO

st.set_page_config(
    page_title="YOLO-AMC Inspection Platform",
    layout="wide"
)

# ============================================================
# Config
# ============================================================

# Change this to your own n8n webhook URL if needed.
N8N_WEBHOOK_URL = "http://localhost:5678/webhook/yoloamc"

BASE_DIR = Path(__file__).resolve().parent

IMAGE_SAVE_DIR = BASE_DIR / "image_save"
RESULT_DIR = BASE_DIR / "result"

MAX_IMAGE_DISPLAY_WIDTH = 500

IMAGE_SAVE_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

VALID_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
MODEL_PREFIXES = ("GAM_", "SA_")
RESULT_SUFFIXES = ("_result",)


# ============================================================
# Helper Functions
# ============================================================

def clear_image_save_dir():
    IMAGE_SAVE_DIR.mkdir(parents=True, exist_ok=True)

    for item in IMAGE_SAVE_DIR.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)


def find_latest_image_in_dir(folder_path):
    folder = Path(folder_path)

    if not folder.exists():
        return None

    image_files = []
    for ext in VALID_IMAGE_EXTS:
        image_files.extend(folder.rglob(f"*{ext}"))

    valid_files = [p for p in image_files if p.exists() and p.is_file()]

    if not valid_files:
        return None

    return max(valid_files, key=lambda p: p.stat().st_mtime)


def find_all_images_in_dir(folder_path):
    folder = Path(folder_path)

    if not folder.exists():
        return []

    image_files = []
    for ext in VALID_IMAGE_EXTS:
        image_files.extend(folder.rglob(f"*{ext}"))

    valid_files = [p for p in image_files if p.exists() and p.is_file()]

    # Do not rely on mtime for pairing; this is only fallback display order.
    valid_files.sort(key=lambda p: p.name.lower())

    return valid_files


def normalize_stem_for_match(path_or_name):
    """Normalize original/result filename stems for Windows and Pi outputs.

    Original:
        1-7.jpg -> 1-7

    Windows result:
        GAM_1-7.jpg -> 1-7
        SA_1-7.jpg -> 1-7

    Pi result:
        1-7_result.jpg -> 1-7
    """
    stem = Path(str(path_or_name)).stem

    for prefix in MODEL_PREFIXES:
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break

    for suffix in RESULT_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break

    return stem.lower().strip()


def build_result_map_by_filename(result_images):
    result_map = {}

    for result_image in result_images:
        key = normalize_stem_for_match(result_image)
        result_map.setdefault(key, []).append(str(result_image))

    # If duplicate keys exist, use stable filename order.
    for key in result_map:
        result_map[key].sort(key=lambda p: Path(p).name.lower())

    return result_map


def match_result_images_to_uploaded_images(uploaded_images, result_images, output_dir):
    """Return {index: result_path}, {index: output_dir} using filename matching.

    This prevents wrong pairing when n8n/Flask/Pi returns result_images in a
    different order from Streamlit's upload order.
    """
    result_map = build_result_map_by_filename(result_images)
    matched_results = {}
    matched_dirs = {}
    used_paths = set()

    for i, image_info in enumerate(uploaded_images):
        original_key = normalize_stem_for_match(image_info["name"])
        candidates = result_map.get(original_key, [])

        selected = None
        for candidate in candidates:
            if candidate not in used_paths:
                selected = candidate
                break

        if selected:
            matched_results[i] = selected
            matched_dirs[i] = output_dir
            used_paths.add(selected)

    return matched_results, matched_dirs


def extract_output_dir(data):
    if not isinstance(data, dict):
        return None

    if data.get("output_dir"):
        return data.get("output_dir")

    if data.get("local_dir"):
        return data.get("local_dir")

    if data.get("history_item") and isinstance(data["history_item"], dict):
        return data["history_item"].get("output_dir")

    if data.get("records") and isinstance(data["records"], list):
        if len(data["records"]) > 0 and isinstance(data["records"][0], dict):
            return data["records"][0].get("output_dir")

    return None


def extract_reply_text(data):
    if not isinstance(data, dict):
        return str(data)

    if data.get("reply"):
        return data.get("reply")

    if data.get("message"):
        return data.get("message")

    status = data.get("status", "unknown")
    source = data.get("source", "")
    model = data.get("model", "")
    avg_conf = data.get("avg_confidence", None)
    exec_time = data.get("execution_time_sec", None)
    total_images = data.get("total_images", None)
    detected_images = data.get("detected_images", None)
    total_boxes = data.get("total_boxes", None)
    output_dir = data.get("output_dir", "") or data.get("local_dir", "")

    lines = []
    lines.append(f"狀態：{status}")

    if source:
        lines.append(f"來源：{source}")

    if model:
        lines.append(f"模型：{model}")

    if total_images is not None:
        lines.append(f"總影像數：{total_images}")

    if detected_images is not None:
        lines.append(f"偵測到裂縫影像數：{detected_images}")

    if total_boxes is not None:
        lines.append(f"總偵測框數：{total_boxes}")

    if avg_conf is not None:
        lines.append(f"平均信心值：{avg_conf}")

    if exec_time is not None:
        lines.append(f"執行時間：{exec_time} 秒")

    if output_dir:
        lines.append(f"輸出資料夾：{output_dir}")

    if len(lines) > 1:
        return "\n".join(lines)

    return str(data)


def normalize_n8n_response(data):
    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], dict):
            return data[0]

    if isinstance(data, dict):
        return data

    return {"reply": str(data)}


def find_result_images_from_response(data):
    if not isinstance(data, dict):
        return [], None

    output_dir = extract_output_dir(data)

    if data.get("result_images") and isinstance(data["result_images"], list):
        valid_images = []

        for p in data["result_images"]:
            image_path = Path(p)

            if image_path.exists() and image_path.is_file():
                valid_images.append(str(image_path))

        if valid_images:
            valid_images.sort(key=lambda p: Path(p).name.lower())
            return valid_images, output_dir

    if data.get("latest_result_image"):
        image_path = Path(data["latest_result_image"])

        if image_path.exists() and image_path.is_file():
            return [str(image_path)], str(image_path.parent)

    if output_dir:
        images = find_all_images_in_dir(output_dir)

        if images:
            return [str(p) for p in images], output_dir

    return [], output_dir


def show_image_file(image_path, caption=None):
    image_path = Path(image_path)

    if not image_path.exists():
        st.error(f"找不到圖片：{image_path}")
        return

    image = Image.open(image_path)
    display_width = min(image.width, MAX_IMAGE_DISPLAY_WIDTH)

    st.image(
        image,
        caption=caption if caption else str(image_path),
        width=display_width
    )


def show_image_bytes(image_bytes, caption=None):
    image = Image.open(BytesIO(image_bytes))
    display_width = min(image.width, MAX_IMAGE_DISPLAY_WIDTH)

    st.image(
        image,
        caption=caption,
        width=display_width
    )


def send_to_n8n(chat_input, image_path=None):
    payload = {
        "chatInput": chat_input,
        "image_path": image_path
    }

    response = requests.post(
        N8N_WEBHOOK_URL,
        json=payload,
        timeout=300
    )

    response.raise_for_status()

    try:
        raw_data = response.json()
        data = normalize_n8n_response(raw_data)
    except Exception:
        data = {"reply": response.text}

    return data


def send_batch_to_n8n(chat_input, image_paths):
    payload = {
        "chatInput": chat_input,
        "image_dir": str(IMAGE_SAVE_DIR),
        "image_paths": image_paths,
        "total_images": len(image_paths),
        "batch_mode": True
    }

    response = requests.post(
        N8N_WEBHOOK_URL,
        json=payload,
        timeout=300
    )

    response.raise_for_status()

    try:
        raw_data = response.json()
        data = normalize_n8n_response(raw_data)
    except Exception:
        data = {"reply": response.text}

    return data


def save_uploaded_images_to_dir():
    clear_image_save_dir()

    image_paths = []

    for image_info in st.session_state.uploaded_images:
        save_path = IMAGE_SAVE_DIR / image_info["name"]

        with open(save_path, "wb") as f:
            f.write(image_info["bytes"])

        image_paths.append(str(save_path))

    return image_paths


# ============================================================
# UI State
# ============================================================

if "messages" not in st.session_state:
    st.session_state.messages = []

if "uploaded_images" not in st.session_state:
    st.session_state.uploaded_images = []

if "uploaded_file_names" not in st.session_state:
    st.session_state.uploaded_file_names = []

if "current_index" not in st.session_state:
    st.session_state.current_index = 0

if "result_images" not in st.session_state:
    st.session_state.result_images = {}

if "output_dirs" not in st.session_state:
    st.session_state.output_dirs = {}

if "show_original" not in st.session_state:
    st.session_state.show_original = False

if "unmatched_result_images" not in st.session_state:
    st.session_state.unmatched_result_images = []


# ============================================================
# Main UI
# ============================================================

st.title("YOLO-AMC Inspection Platform")
st.caption("Streamlit UI → n8n Agent → YOLO-AMC Detection")

left_col, right_col = st.columns([1, 1])

# ============================================================
# Left: Chat Only
# ============================================================

with left_col:
    st.subheader("Chat Interface")

    if st.session_state.messages:
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.write(message["content"])
    else:
        st.info("請輸入指令，例如：執行 GAM 裂縫偵測、說明 confidence、查詢最近一次紀錄。")


# ============================================================
# Right: Result Viewer + Upload
# ============================================================

with right_col:
    st.subheader("Detection Result Viewer")

    if st.session_state.uploaded_images:
        current = st.session_state.current_index
        total = len(st.session_state.uploaded_images)

        image_info = st.session_state.uploaded_images[current]
        result_path = st.session_state.result_images.get(current)
        output_dir = st.session_state.output_dirs.get(current)

        st.caption(f"目前顯示：第 {current + 1} / {total} 張")

        toggle_col1, toggle_col2 = st.columns([1, 1])

        with toggle_col1:
            if st.button("查看原圖", use_container_width=True):
                st.session_state.show_original = True
                st.rerun()

        with toggle_col2:
            if st.button("查看偵測圖", use_container_width=True):
                st.session_state.show_original = False
                st.rerun()

        nav_col1, nav_col2, nav_col3 = st.columns([1, 1, 1])

        with nav_col1:
            if st.button("上一張", use_container_width=True):
                if st.session_state.current_index > 0:
                    st.session_state.current_index -= 1
                    st.session_state.show_original = False
                    st.rerun()

        with nav_col2:
            if st.button("重設到第一張", use_container_width=True):
                st.session_state.current_index = 0
                st.session_state.show_original = False
                st.rerun()

        with nav_col3:
            if st.button("下一張", use_container_width=True):
                if st.session_state.current_index < total - 1:
                    st.session_state.current_index += 1
                    st.session_state.show_original = False
                    st.rerun()

        if st.session_state.show_original:
            show_image_bytes(
                image_info["bytes"],
                caption=f"Original: {image_info['name']}"
            )

        else:
            if result_path:
                result_path_obj = Path(result_path)

                if result_path_obj.exists():
                    show_image_file(
                        result_path_obj,
                        caption=f"Result: {result_path_obj}"
                    )

                    if output_dir:
                        st.caption(f"Output folder: {output_dir}")
                else:
                    st.error(f"找不到結果圖片：{result_path_obj}")
                    st.session_state.result_images.pop(current, None)
                    st.session_state.output_dirs.pop(current, None)

            else:
                st.info("這張圖片尚未偵測，偵測後結果會顯示在這裡。")

        if st.session_state.unmatched_result_images:
            with st.expander("未配對結果圖片"):
                for p in st.session_state.unmatched_result_images:
                    st.caption(str(p))

    else:
        st.info("Detection result image will be shown here after detection.")

    st.divider()

    st.subheader("Image Upload")

    uploaded_files = st.file_uploader(
        "Upload crack images",
        type=["jpg", "jpeg", "png", "bmp", "webp"],
        accept_multiple_files=True
    )

    if uploaded_files:
        new_file_names = [file.name for file in uploaded_files]

        if st.session_state.uploaded_file_names != new_file_names:
            clear_image_save_dir()

            saved_files = []

            for uploaded_file in uploaded_files:
                saved_files.append({
                    "name": uploaded_file.name,
                    "bytes": uploaded_file.getvalue()
                })

            st.session_state.uploaded_images = saved_files
            st.session_state.uploaded_file_names = new_file_names
            st.session_state.current_index = 0
            st.session_state.result_images = {}
            st.session_state.output_dirs = {}
            st.session_state.show_original = False
            st.session_state.unmatched_result_images = []

        total = len(st.session_state.uploaded_images)
        current = st.session_state.current_index
        current_image = st.session_state.uploaded_images[current]

        st.success(f"已上傳 {total} 張圖片")
        st.caption(f"目前圖片：第 {current + 1} / {total} 張")
        st.caption(f"檔名：{current_image['name']}")

    else:
        st.info("請上傳一張或多張裂縫圖片。")


# ============================================================
# Chat Input
# ============================================================

user_input = st.chat_input("輸入指令，例如：執行 GAM 裂縫偵測")

if user_input:
    st.session_state.messages.append({
        "role": "user",
        "content": user_input
    })

    try:
        if st.session_state.uploaded_images:
            total_images = len(st.session_state.uploaded_images)

            progress_bar = st.progress(0)
            status_text = st.empty()

            with st.spinner("YOLO-AMC Agent is processing batch images..."):
                status_text.write(f"正在批次送出 {total_images} 張圖片...")

                image_paths = save_uploaded_images_to_dir()

                data = send_batch_to_n8n(user_input, image_paths)

                reply = extract_reply_text(data)
                result_images, output_dir = find_result_images_from_response(data)

                st.session_state.result_images = {}
                st.session_state.output_dirs = {}
                st.session_state.unmatched_result_images = []

                if result_images:
                    matched_results, matched_dirs = match_result_images_to_uploaded_images(
                        st.session_state.uploaded_images,
                        result_images,
                        output_dir
                    )

                    st.session_state.result_images = matched_results
                    st.session_state.output_dirs = matched_dirs

                    used = set(matched_results.values())
                    st.session_state.unmatched_result_images = [
                        p for p in result_images if p not in used
                    ]

                    matched_count = len(matched_results)
                    if matched_count < total_images:
                        warning = (
                            f"\n\n提醒：已依檔名配對 {matched_count} / {total_images} 張結果圖。"
                            "若有圖片尚未顯示，請確認輸出檔名是否保留原始檔名。"
                        )
                        reply = f"{reply}{warning}"

                progress_bar.progress(1.0)
                status_text.write("批次偵測完成。")

            st.session_state.current_index = 0
            st.session_state.show_original = False

        else:
            with st.spinner("YOLO-AMC Agent is processing..."):
                data = send_to_n8n(user_input, None)

            reply = extract_reply_text(data)

        st.session_state.messages.append({
            "role": "assistant",
            "content": reply
        })

        st.rerun()

    except Exception as e:
        reply = f"n8n 連線失敗：{e}"

        st.session_state.messages.append({
            "role": "assistant",
            "content": reply
        })
