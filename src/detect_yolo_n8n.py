from ultralytics.models import YOLO
from ultralytics.nn.modules.conv import GAM_Attention

import os
import cv2
import time
import json
import sys

print("=== RUNNING detect_yolo_n8n.py ===")

# ============================================================
# receive model parameter
# ============================================================

selected_model = sys.argv[1]

# ============================================================
# Model Configuration
# Place your trained model weights in the "weights" folder.
# ============================================================

MODELS = {
    "GAM": "weights/GAM_best.pt",
    "SA": "weights/SA_best.pt"
}

if selected_model not in MODELS:
    print(f"Unknown model: {selected_model}")
    sys.exit(1)

model_path = MODELS[selected_model]

if not os.path.exists(model_path):
    raise FileNotFoundError(
        f"Model not found: {model_path}\n"
        "Please place your trained model weights in the 'weights' folder."
    )

# ============================================================
# validate model
# ============================================================

if selected_model not in MODELS:
    print(f"Unknown model: {selected_model}")
    sys.exit(1)

model_path = MODELS[selected_model]

print("\n==============================")
print("Selected Model:", selected_model)
print("Model Path:", model_path)
print("==============================")

# ============================================================
# paths
# ============================================================

IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset", "images")

BASE_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result")

timestamp = time.strftime("%Y%m%d_%H%M%S")

OUTPUT_DIR = os.path.join(BASE_OUTPUT_DIR, f"{timestamp}_{selected_model}")

SUMMARY_PATH = os.path.join(OUTPUT_DIR, "summary.json")

os.makedirs(OUTPUT_DIR, exist_ok=True)

LIMIT = None

CONF_THRESHOLD = 0.75

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# ============================================================
# helper
# ============================================================

def get_image_files(image_dir, limit=None):

    valid_exts = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

    image_files = [
        os.path.join(image_dir, f)
        for f in os.listdir(image_dir)
        if f.lower().endswith(valid_exts)
    ]

    image_files.sort()

    if limit is not None:
        image_files = image_files[:limit]

    return image_files

# ============================================================
# main
# ============================================================

start_time = time.time()

image_files = get_image_files(IMAGE_DIR, LIMIT)

print("Image folder:", IMAGE_DIR)
print("Total images:", len(image_files))
print("Output folder:", OUTPUT_DIR)

if len(image_files) == 0:
    print("No images found.")
    sys.exit(1)

# ============================================================
# load model
# ============================================================

model = YOLO(model_path)

saved_files = []

all_confidences = []

detected_images = 0

total_boxes = 0

# ============================================================
# inference
# ============================================================

for img_path in image_files:

    print("Predicting:", img_path)

    base_name = os.path.splitext(os.path.basename(img_path))[0]

    results = model.predict(
        source=img_path,
        device="0",
        imgsz=640,
        conf=CONF_THRESHOLD,
        save=False,
        verbose=False
    )

    result = results[0]

    boxes = result.boxes

    num_boxes = 0

    image_confidences = []

    if boxes is not None and boxes.conf is not None:

        conf_values = boxes.conf.detach().cpu().numpy().tolist()

        image_confidences = [float(c) for c in conf_values]

        num_boxes = len(image_confidences)

    if num_boxes > 0:

        detected_images += 1

        total_boxes += num_boxes

        all_confidences.extend(image_confidences)

    result_img = result.plot(
        labels=True,
        conf=True,
        line_width=2
    )

    save_name = f"{selected_model}_{base_name}.jpg"

    save_path = os.path.join(OUTPUT_DIR, save_name)

    success = cv2.imwrite(save_path, result_img)

    if success:
        saved_files.append(save_path)
        print("Saved:", save_path)

    else:
        print("Failed to save:", save_path)

# ============================================================
# summary
# ============================================================

execution_time_sec = round(time.time() - start_time, 2)

avg_confidence = (
    round(sum(all_confidences) / len(all_confidences), 4)
    if len(all_confidences) > 0
    else 0
)

summary = {
    "status": "success",
    "task": "YOLO-AMC crack detection",
    "timestamp": timestamp,
    "model": selected_model,
    "confidence_threshold": CONF_THRESHOLD,
    "total_images": len(image_files),
    "detected_images": detected_images,
    "total_boxes": total_boxes,
    "avg_confidence": avg_confidence,
    "output_dir": OUTPUT_DIR,
    "summary_path": SUMMARY_PATH,
    "saved_files": saved_files,
    "execution_time_sec": execution_time_sec
}

with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print("\nSummary saved:", SUMMARY_PATH)

print(json.dumps(summary, ensure_ascii=False, indent=2))

print("\nAll done.")