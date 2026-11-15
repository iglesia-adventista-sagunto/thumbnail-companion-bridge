import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

import script as engine

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
)
logger = logging.getLogger("thumbnail-bridge")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATE_CONFIG_PATH = os.path.join(BASE_DIR, "config.template.json")
VERSION = "1.0.0"

state_lock = threading.Lock()
render_lock = threading.Lock()

job_state = {
    "state": "idle",
    "busy": False,
    "last_generated_at": None,
    "last_title": None,
    "last_subtitle": None,
    "last_preset": None,
    "last_file": None,
    "last_files": [],
    "last_error": None,
    "total_generated": 0,
}


def _resolve_config_path():
    return os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)


def _resolve_dir(path_val, default_rel):
    raw = path_val or default_rel
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(BASE_DIR, raw))


def load_config():
    """Load service configuration from CONFIG_PATH (or template/defaults) with env overrides."""
    config_path = _resolve_config_path()
    data = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded configuration from %s", config_path)
    elif os.path.exists(TEMPLATE_CONFIG_PATH):
        with open(TEMPLATE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.warning("config.json not found at %s; using config.template.json defaults", config_path)
    else:
        logger.warning("No config.json or template found; using built-in defaults")

    port = int(os.environ.get("PORT") or data.get("port", 5000))
    output_dir = _resolve_dir(os.environ.get("OUTPUT_DIR") or data.get("output_dir"), "thumbnails")
    attributions_file = _resolve_dir(
        os.environ.get("ATTRIBUTIONS_FILE") or data.get("attributions_file"),
        os.path.join(output_dir, "attributions.txt"),
    )
    default_website = os.environ.get("DEFAULT_WEBSITE") or data.get("default_website", "sagunto.adventista.es")
    unsplash_key = (os.environ.get("UNSPLASH_ACCESS_KEY") or data.get("unsplash_access_key") or "").strip()
    watermark_file = os.environ.get("WATERMARK_FILE") or data.get("watermark_file") or "assets/right_sidebar_watermark.png"
    logo_file = os.environ.get("LOGO_FILE") or data.get("logo_file") or "assets/logo_watermark_bold.png"
    font_file = os.environ.get("FONT_FILE") or data.get("font_file") or ""

    presets = {}
    for item in data.get("presets", []):
        name = str(item.get("name", "")).strip()
        if name:
            presets[name.lower()] = {
                "name": name,
                "title": item.get("title", ""),
                "subtitle": item.get("subtitle", ""),
                "query": item.get("query", "default"),
                "filename": item.get("filename", name),
                "variations": max(1, int(item.get("variations", 1))),
                "site_url": item.get("site_url") or default_website,
            }

    return {
        "port": port,
        "output_dir": output_dir,
        "attributions_file": attributions_file,
        "default_website": default_website,
        "unsplash_access_key": unsplash_key,
        "watermark_file": watermark_file,
        "logo_file": logo_file,
        "font_file": font_file or None,
        "presets": presets,
    }


cfg = load_config()
os.makedirs(cfg["output_dir"], exist_ok=True)

app = Flask(__name__)


def resolve_preset(name):
    if not name:
        return None
    return cfg["presets"].get(str(name).strip().lower())


def _parse_bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _extract_params():
    params = {}
    if request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            params.update(body)
    if request.form:
        params.update(request.form.to_dict())
    if request.args:
        params.update(request.args.to_dict())
    return params


def _list_png_files():
    out_dir = cfg["output_dir"]
    if not os.path.isdir(out_dir):
        return []
    entries = []
    for name in os.listdir(out_dir):
        if not name.lower().endswith(".png"):
            continue
        full_path = os.path.join(out_dir, name)
        if os.path.isfile(full_path):
            stat = os.stat(full_path)
            entries.append(
                {
                    "filename": name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "mtime": stat.st_mtime,
                    "url": f"/thumbnails/{name}",
                }
            )
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    for item in entries:
        item.pop("mtime", None)
    return entries


def _run_generation(title, subtitle, query, filename, variations, site_url, preset_name=None):
    with state_lock:
        job_state["state"] = "generating"
        job_state["busy"] = True
        job_state["last_error"] = None

    try:
        with render_lock:
            result = engine.generate_thumbnails(
                title=title,
                subtitle=subtitle,
                keyword_or_url=query,
                filename=filename,
                variations=variations,
                site_url=site_url,
                output_dir=cfg["output_dir"],
                attributions_file=cfg["attributions_file"],
                unsplash_access_key=cfg["unsplash_access_key"],
                watermark_file=cfg["watermark_file"],
                logo_file=cfg["logo_file"],
                font_file=cfg["font_file"],
                append_attributions=True,
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        with state_lock:
            job_state["state"] = "ready"
            job_state["busy"] = False
            job_state["last_generated_at"] = now_iso
            job_state["last_title"] = result["title"]
            job_state["last_subtitle"] = result["subtitle"]
            job_state["last_preset"] = preset_name
            job_state["last_files"] = result["files"]
            job_state["last_file"] = result["files"][0] if result["files"] else None
            job_state["total_generated"] += len(result["files"])

        return result
    except Exception as e:
        logger.exception("Thumbnail generation failed: %s", e)
        with state_lock:
            job_state["state"] = "error"
            job_state["busy"] = False
            job_state["last_error"] = str(e)
        raise


def _handle_generate(preset=None):
    params = _extract_params()

    if not preset and params.get("preset"):
        preset = resolve_preset(params.get("preset"))
        if not preset:
            return jsonify({"error": f"Preset '{params.get('preset')}' not found"}), 404

    title = params.get("title") if params.get("title") is not None else (preset["title"] if preset else "")
    title = str(title or "").strip()
    if not title:
        return jsonify({"error": "Missing required parameter: title"}), 400

    subtitle = params.get("subtitle") if params.get("subtitle") is not None else (preset["subtitle"] if preset else "")
    query = (
        params.get("query")
        or params.get("keyword")
        or params.get("url")
        or (preset["query"] if preset else "default")
    )
    filename = params.get("filename") or (preset["filename"] if preset else None)
    site_url = params.get("site_url") or (preset["site_url"] if preset else cfg["default_website"])

    raw_variations = params.get("variations") if params.get("variations") is not None else (preset["variations"] if preset else 1)
    try:
        variations = max(1, min(10, int(raw_variations)))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid 'variations' parameter; must be an integer"}), 400

    is_async = _parse_bool(params.get("async"), default=False)
    preset_name = preset["name"] if preset else None

    if is_async:
        thread = threading.Thread(
            target=_run_generation,
            args=(title, subtitle, query, filename, variations, site_url, preset_name),
            daemon=True,
        )
        thread.start()
        return jsonify({
            "status": "accepted",
            "state": "generating",
            "title": title,
            "subtitle": str(subtitle or "").strip(),
            "preset": preset_name,
            "variations": variations,
        }), 202

    try:
        result = _run_generation(title, subtitle, query, filename, variations, site_url, preset_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "status": "ok",
        "preset": preset_name,
        "title": result["title"],
        "subtitle": result["subtitle"],
        "query": result["query"],
        "files": result["files"],
        "urls": [f"/thumbnails/{f}" for f in result["files"]],
        "latest_url": "/latest",
        "attributions": result["attributions"],
    }), 200


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    with state_lock:
        snapshot = dict(job_state)
    return jsonify({
        "status": "healthy",
        "service": "automatic-thumbnail",
        "version": VERSION,
        "port": cfg["port"],
        "output_dir": cfg["output_dir"],
        "font_loaded": bool(cfg["font_file"] or engine.FONT_FILE),
        "unsplash_configured": bool(cfg["unsplash_access_key"] or engine.UNSPLASH_ACCESS_KEY),
        "presets_count": len(cfg["presets"]),
        "busy": snapshot["busy"],
        "state": snapshot["state"],
        "last_file": snapshot["last_file"],
    }), 200


@app.route("/status", methods=["GET"])
def status():
    with state_lock:
        snapshot = dict(job_state)
    return jsonify(snapshot), 200


@app.route("/is_busy", methods=["GET"])
def is_busy():
    with state_lock:
        busy = job_state["busy"]
    return ("true" if busy else "false"), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/is_ready", methods=["GET"])
def is_ready():
    with state_lock:
        busy = job_state["busy"]
        has_last = bool(job_state["last_file"])
    ready = (not busy) and (has_last or len(_list_png_files()) > 0)
    return ("true" if ready else "false"), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/presets", methods=["GET"])
def list_presets():
    return jsonify({"presets": list(cfg["presets"].values())}), 200


@app.route("/generate", methods=["GET", "POST"])
def generate_endpoint():
    return _handle_generate(preset=None)


@app.route("/preset/<preset_name>/generate", methods=["GET", "POST"])
@app.route("/<preset_name>/generate", methods=["GET", "POST"])
def generate_preset_endpoint(preset_name):
    preset = resolve_preset(preset_name)
    if not preset:
        return jsonify({"error": f"Preset '{preset_name}' not found"}), 404
    return _handle_generate(preset=preset)


@app.route("/batch", methods=["GET", "POST"])
def batch_endpoint():
    params = _extract_params()
    csv_content = params.get("csv_content")
    csv_path = params.get("csv_path") or "miniaturas.csv"
    is_async = _parse_bool(params.get("async"), default=False)

    def _run_batch():
        temp_path = None
        target_csv = csv_path
        try:
            if csv_content:
                fd, temp_path = tempfile.mkstemp(suffix=".csv", prefix="batch_")
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(csv_content)
                target_csv = temp_path

            with state_lock:
                job_state["state"] = "generating"
                job_state["busy"] = True
                job_state["last_error"] = None

            with render_lock:
                res = engine.batch_process(
                    csv_path=target_csv,
                    output_dir=cfg["output_dir"],
                    attributions_file=cfg["attributions_file"],
                    site_url=cfg["default_website"],
                    unsplash_access_key=cfg["unsplash_access_key"],
                    watermark_file=cfg["watermark_file"],
                    logo_file=cfg["logo_file"],
                    font_file=cfg["font_file"],
                )

            if "error" in res:
                with state_lock:
                    job_state["state"] = "error"
                    job_state["busy"] = False
                    job_state["last_error"] = res["error"]
                return res

            now_iso = datetime.now(timezone.utc).isoformat()
            with state_lock:
                job_state["state"] = "ready"
                job_state["busy"] = False
                job_state["last_generated_at"] = now_iso
                job_state["last_files"] = res["files"]
                job_state["last_file"] = res["files"][-1] if res["files"] else None
                job_state["total_generated"] += len(res["files"])
            return res
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    if is_async:
        threading.Thread(target=_run_batch, daemon=True).start()
        return jsonify({"status": "accepted", "state": "generating"}), 202

    res = _run_batch()
    if "error" in res:
        return jsonify(res), 404
    return jsonify({
        "status": "ok",
        "rows_processed": res["rows_processed"],
        "files": res["files"],
        "urls": [f"/thumbnails/{f}" for f in res["files"]],
    }), 200


@app.route("/thumbnails", methods=["GET"])
def list_thumbnails():
    files = _list_png_files()
    return jsonify({"count": len(files), "thumbnails": files}), 200


def _safe_thumbnail_name(filename):
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if not filename.lower().endswith(".png"):
        return None
    return os.path.basename(filename)


@app.route("/thumbnails/<filename>", methods=["GET"])
def get_thumbnail(filename):
    safe_name = _safe_thumbnail_name(filename)
    if not safe_name:
        return jsonify({"error": "Invalid thumbnail filename"}), 400
    full_path = os.path.join(cfg["output_dir"], safe_name)
    if not os.path.isfile(full_path):
        return jsonify({"error": "Thumbnail not found"}), 404
    resp = send_from_directory(cfg["output_dir"], safe_name, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/thumbnails/<filename>", methods=["DELETE"])
def delete_thumbnail(filename):
    safe_name = _safe_thumbnail_name(filename)
    if not safe_name:
        return jsonify({"error": "Invalid thumbnail filename"}), 400
    full_path = os.path.join(cfg["output_dir"], safe_name)
    if not os.path.isfile(full_path):
        return jsonify({"error": "Thumbnail not found"}), 404
    os.remove(full_path)
    return jsonify({"status": "deleted", "filename": safe_name}), 200


@app.route("/latest", methods=["GET"])
def get_latest_thumbnail():
    files = _list_png_files()
    if not files:
        return jsonify({"error": "No thumbnails generated yet"}), 404
    latest = files[0]
    if request.args.get("format") == "json":
        return jsonify(latest), 200
    resp = send_from_directory(cfg["output_dir"], latest["filename"], mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/attributions", methods=["GET"])
def get_attributions():
    attr_file = cfg["attributions_file"]
    content = ""
    if os.path.exists(attr_file):
        with open(attr_file, "r", encoding="utf-8") as f:
            content = f.read()
    if request.args.get("format") == "json":
        return jsonify({"attributions_file": attr_file, "content": content}), 200
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/reload", methods=["POST"])
def reload_config():
    global cfg
    cfg = load_config()
    os.makedirs(cfg["output_dir"], exist_ok=True)
    return jsonify({"status": "reloaded", "presets_count": len(cfg["presets"])}), 200


def main():
    logger.info("Starting Automatic Thumbnail Bridge v%s on port %d...", VERSION, cfg["port"])
    app.run(host="0.0.0.0", port=cfg["port"], threaded=True)


if __name__ == "__main__":
    main()
