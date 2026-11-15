import csv
import io
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory

import script as engine
from youtube_service import (
    YouTubeService,
    format_livestream_title,
    update_description_thumbnail_license,
)

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
    csv_path = _resolve_dir(os.environ.get("CSV_PATH") or data.get("csv_path"), "miniaturas.csv")

    yt_cfg = data.get("youtube") if isinstance(data.get("youtube"), dict) else {}
    youtube_client_secrets = _resolve_dir(
        os.environ.get("YOUTUBE_CLIENT_SECRETS") or yt_cfg.get("client_secrets_file"),
        "client_secrets.json",
    )
    youtube_token_file = _resolve_dir(
        os.environ.get("YOUTUBE_TOKEN_FILE") or yt_cfg.get("token_file"),
        "youtube_token.json",
    )
    youtube_default_broadcast_id = (
        os.environ.get("YOUTUBE_BROADCAST_ID") or yt_cfg.get("default_broadcast_id") or ""
    ).strip()

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
        "csv_path": csv_path,
        "youtube": {
            "client_secrets_file": youtube_client_secrets,
            "token_file": youtube_token_file,
            "default_broadcast_id": youtube_default_broadcast_id,
        },
        "presets": presets,
    }


def save_presets_to_config():
    """Persist current presets into the active config.json file."""
    config_path = _resolve_config_path()
    data = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif os.path.exists(TEMPLATE_CONFIG_PATH):
        with open(TEMPLATE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

    data["presets"] = list(cfg["presets"].values())
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


cfg = load_config()
os.makedirs(cfg["output_dir"], exist_ok=True)
yt_service = YouTubeService(
    cfg["youtube"]["client_secrets_file"],
    cfg["youtube"]["token_file"],
)

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


def _state_file_path():
    return os.path.join(cfg["output_dir"], ".thumbnail_state.json")


def _load_thumbnail_state():
    path = _state_file_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("pinned_latest", None)
                data.setdefault("thumbnails", {})
                return data
        except Exception as e:
            logger.warning("Failed to read thumbnail state: %s", e)
    return {"pinned_latest": None, "thumbnails": {}}


def _save_thumbnail_state(data):
    os.makedirs(cfg["output_dir"], exist_ok=True)
    with open(_state_file_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _record_thumbnails_metadata(items, pinned_filename=None):
    """Persist per-thumbnail metadata and optionally update pinned_latest."""
    with state_lock:
        tstate = _load_thumbnail_state()
        now_iso = datetime.now(timezone.utc).isoformat()
        for fname, meta in items.items():
            tstate["thumbnails"][fname] = {
                "title": meta.get("title", ""),
                "subtitle": meta.get("subtitle", ""),
                "query": meta.get("query", ""),
                "preset": meta.get("preset"),
                "attribution": meta.get("attribution", ""),
                "created_at": meta.get("created_at") or now_iso,
            }
        if pinned_filename:
            tstate["pinned_latest"] = pinned_filename
        _save_thumbnail_state(tstate)


def _set_pinned_latest(filename):
    with state_lock:
        tstate = _load_thumbnail_state()
        tstate["pinned_latest"] = filename
        _save_thumbnail_state(tstate)
        job_state["last_file"] = filename
        meta = tstate["thumbnails"].get(filename, {})
        if meta.get("title"):
            job_state["last_title"] = meta["title"]
            job_state["last_subtitle"] = meta.get("subtitle", "")


def _get_active_latest():
    """Return (latest_entry_dict, is_pinned) or (None, False) if no thumbnails exist."""
    files = _list_png_files()
    if not files:
        return None, False

    with state_lock:
        tstate = _load_thumbnail_state()
    pinned = tstate.get("pinned_latest")
    if pinned:
        for entry in files:
            if entry["filename"] == pinned:
                return entry, True
    return files[0], False


def _list_png_files():
    out_dir = cfg["output_dir"]
    if not os.path.isdir(out_dir):
        return []
    with state_lock:
        tstate = _load_thumbnail_state()
    pinned = tstate.get("pinned_latest")
    meta_map = tstate.get("thumbnails", {})

    entries = []
    for name in os.listdir(out_dir):
        if not name.lower().endswith(".png"):
            continue
        full_path = os.path.join(out_dir, name)
        if os.path.isfile(full_path):
            stat = os.stat(full_path)
            meta = meta_map.get(name, {})
            entries.append(
                {
                    "filename": name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "mtime": stat.st_mtime,
                    "url": f"/thumbnails/{name}",
                    "is_latest": False,
                    "title": meta.get("title", ""),
                    "subtitle": meta.get("subtitle", ""),
                    "attribution": meta.get("attribution", ""),
                    "preset": meta.get("preset"),
                }
            )
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    for item in entries:
        item.pop("mtime", None)

    active_name = None
    if pinned and any(e["filename"] == pinned for e in entries):
        active_name = pinned
    elif entries:
        active_name = entries[0]["filename"]

    for item in entries:
        item["is_latest"] = item["filename"] == active_name
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
        first_file = result["files"][0] if result["files"] else None
        meta_items = {}
        file_attrs = result.get("file_attributions", {})
        for fname in result["files"]:
            meta_items[fname] = {
                "title": result["title"],
                "subtitle": result["subtitle"],
                "query": result["query"],
                "preset": preset_name,
                "attribution": file_attrs.get(fname, ""),
                "created_at": now_iso,
            }
        _record_thumbnails_metadata(meta_items, pinned_filename=first_file)

        with state_lock:
            job_state["state"] = "ready"
            job_state["busy"] = False
            job_state["last_generated_at"] = now_iso
            job_state["last_title"] = result["title"]
            job_state["last_subtitle"] = result["subtitle"]
            job_state["last_preset"] = preset_name
            job_state["last_files"] = result["files"]
            job_state["last_file"] = first_file
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


@app.route("/ui", methods=["GET"])
def web_ui():
    return render_template("index.html")


@app.route("/", methods=["GET"])
def index_or_health():
    if "text/html" in request.headers.get("Accept", ""):
        return render_template("index.html")
    return health()


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


@app.route("/presets", methods=["POST"])
def upsert_preset():
    params = _extract_params()
    name = str(params.get("name") or "").strip()
    title = str(params.get("title") or "").strip()
    if not name:
        return jsonify({"error": "Missing required parameter: name"}), 400
    if not title:
        return jsonify({"error": "Missing required parameter: title"}), 400

    try:
        variations = max(1, min(10, int(params.get("variations") or 1)))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid 'variations' parameter; must be an integer"}), 400

    preset_obj = {
        "name": name,
        "title": title,
        "subtitle": str(params.get("subtitle") or "").strip(),
        "query": str(params.get("query") or "default").strip() or "default",
        "filename": str(params.get("filename") or engine.sanitize_filename(name)).strip(),
        "variations": variations,
        "site_url": str(params.get("site_url") or cfg["default_website"]).strip(),
    }
    cfg["presets"][name.lower()] = preset_obj
    try:
        save_presets_to_config()
    except Exception as e:
        logger.warning("Failed to persist preset to config: %s", e)

    return jsonify({"status": "saved", "preset": preset_obj}), 200


@app.route("/presets/<preset_name>", methods=["DELETE"])
def delete_preset(preset_name):
    key = str(preset_name or "").strip().lower()
    if key not in cfg["presets"]:
        return jsonify({"error": f"Preset '{preset_name}' not found"}), 404
    removed = cfg["presets"].pop(key)
    try:
        save_presets_to_config()
    except Exception as e:
        logger.warning("Failed to persist preset deletion to config: %s", e)
    return jsonify({"status": "deleted", "preset": removed["name"]}), 200


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
    csv_path = params.get("csv_path") or cfg["csv_path"]
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
            last_file = res["files"][-1] if res["files"] else None
            if res.get("file_metadata"):
                _record_thumbnails_metadata(res["file_metadata"], pinned_filename=last_file)

            with state_lock:
                job_state["state"] = "ready"
                job_state["busy"] = False
                job_state["last_generated_at"] = now_iso
                job_state["last_files"] = res["files"]
                job_state["last_file"] = last_file
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

    with state_lock:
        tstate = _load_thumbnail_state()
        tstate["thumbnails"].pop(safe_name, None)
        if tstate.get("pinned_latest") == safe_name:
            tstate["pinned_latest"] = None
        _save_thumbnail_state(tstate)
        if job_state.get("last_file") == safe_name:
            job_state["last_file"] = None

    return jsonify({"status": "deleted", "filename": safe_name}), 200


@app.route("/latest", methods=["GET"])
def get_latest_thumbnail():
    latest, is_pinned = _get_active_latest()
    if not latest:
        return jsonify({"error": "No thumbnails generated yet"}), 404
    if request.args.get("format") == "json":
        payload = dict(latest)
        payload["pinned"] = is_pinned
        return jsonify(payload), 200
    resp = send_from_directory(cfg["output_dir"], latest["filename"], mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/latest", methods=["POST", "PUT"])
def set_latest_thumbnail():
    params = _extract_params()
    filename = params.get("filename")
    safe_name = _safe_thumbnail_name(filename)
    if not safe_name:
        return jsonify({"error": "Invalid or missing 'filename' parameter"}), 400
    full_path = os.path.join(cfg["output_dir"], safe_name)
    if not os.path.isfile(full_path):
        return jsonify({"error": f"Thumbnail '{safe_name}' not found"}), 404

    _set_pinned_latest(safe_name)
    latest, is_pinned = _get_active_latest()
    return jsonify({
        "status": "ok",
        "latest": safe_name,
        "pinned": is_pinned,
        "url": f"/thumbnails/{safe_name}",
        "latest_url": "/latest",
        "thumbnail": latest,
    }), 200


CSV_DEFAULT_COLUMNS = ["Filename", "Title", "Subtitle", "Unsplash_Keyword_or_URL", "Variations"]


@app.route("/csv", methods=["GET"])
def get_csv():
    csv_path = cfg["csv_path"]
    raw = ""
    rows = []
    columns = list(CSV_DEFAULT_COLUMNS)

    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
        reader = csv.DictReader(io.StringIO(raw))
        if reader.fieldnames:
            columns = [c for c in reader.fieldnames if c]
        for row in reader:
            cleaned = {col: (row.get(col) or "") for col in columns}
            rows.append(cleaned)

    if request.args.get("format") in ("raw", "csv"):
        return raw, 200, {"Content-Type": "text/csv; charset=utf-8"}

    return jsonify({
        "path": csv_path,
        "columns": columns,
        "rows": rows,
        "raw": raw,
    }), 200


@app.route("/csv", methods=["POST", "PUT"])
def save_csv():
    params = _extract_params()
    csv_path = cfg["csv_path"]
    raw_content = params.get("raw") if params.get("raw") is not None else params.get("csv_content")
    rows = params.get("rows")

    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

    if raw_content is not None:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            f.write(str(raw_content))
    elif isinstance(rows, list):
        columns = params.get("columns")
        if not columns or not isinstance(columns, list):
            columns = list(CSV_DEFAULT_COLUMNS)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if isinstance(row, dict):
                writer.writerow({col: row.get(col, "") for col in columns})
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
    else:
        return jsonify({"error": "Provide either 'rows' (list) or 'raw'/'csv_content' (string)"}), 400

    run_now = _parse_bool(params.get("run"), default=False)
    if run_now:
        with render_lock:
            res = engine.batch_process(
                csv_path=csv_path,
                output_dir=cfg["output_dir"],
                attributions_file=cfg["attributions_file"],
                site_url=cfg["default_website"],
                unsplash_access_key=cfg["unsplash_access_key"],
                watermark_file=cfg["watermark_file"],
                logo_file=cfg["logo_file"],
                font_file=cfg["font_file"],
            )
        if "error" in res:
            return jsonify(res), 400
        now_iso = datetime.now(timezone.utc).isoformat()
        last_file = res["files"][-1] if res["files"] else None
        if res.get("file_metadata"):
            _record_thumbnails_metadata(res["file_metadata"], pinned_filename=last_file)
        with state_lock:
            job_state["state"] = "ready"
            job_state["busy"] = False
            job_state["last_generated_at"] = now_iso
            job_state["last_files"] = res["files"]
            job_state["last_file"] = last_file
            job_state["total_generated"] += len(res["files"])
        return jsonify({"status": "saved_and_generated", "rows_processed": res["rows_processed"], "files": res["files"]}), 200

    return jsonify({"status": "saved", "path": csv_path}), 200


@app.route("/youtube/status", methods=["GET"])
def youtube_status():
    status_data = yt_service.get_status()
    status_data["default_broadcast_id"] = cfg["youtube"]["default_broadcast_id"]
    return jsonify(status_data), 200


@app.route("/youtube/auth/start", methods=["GET"])
def youtube_auth_start():
    redirect_uri = request.url_root.rstrip("/") + "/youtube/auth/callback"
    try:
        auth_url, state = yt_service.start_auth(redirect_uri)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    if _parse_bool(request.args.get("redirect"), default=False):
        return redirect(auth_url)
    return jsonify({"auth_url": auth_url, "state": state, "redirect_uri": redirect_uri}), 200


@app.route("/youtube/auth/callback", methods=["GET"])
def youtube_auth_callback():
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    err = request.args.get("error")
    if err:
        return jsonify({"error": f"YouTube OAuth error: {err}"}), 400
    if not code:
        return jsonify({"error": "Missing OAuth 'code' parameter"}), 400
    try:
        yt_service.finish_auth(state, code)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return redirect("/ui?youtube=connected")


@app.route("/youtube/broadcasts", methods=["GET"])
def youtube_broadcasts():
    try:
        items = yt_service.list_broadcasts()
        return jsonify({"broadcasts": items}), 200
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/youtube/preview", methods=["GET", "POST"])
def youtube_preview():
    params = _extract_params()
    latest, _ = _get_active_latest()
    title_raw = params.get("title") if params.get("title") is not None else (latest.get("title", "") if latest else "")
    subtitle_raw = params.get("subtitle") if params.get("subtitle") is not None else (latest.get("subtitle", "") if latest else "")
    license_raw = params.get("license") if params.get("license") is not None else (latest.get("attribution", "") if latest else "")
    base_desc = params.get("description") if params.get("description") is not None else ""

    broadcast_id = (params.get("broadcast_id") or cfg["youtube"]["default_broadcast_id"] or "").strip()
    if not base_desc and broadcast_id and yt_service.get_status().get("authenticated"):
        try:
            snippet = yt_service.get_video_snippet(broadcast_id)
            base_desc = snippet.get("description", "")
        except Exception as e:
            logger.warning("Could not fetch YouTube description for preview: %s", e)

    yt_title = format_livestream_title(title_raw, subtitle_raw)
    yt_desc = update_description_thumbnail_license(base_desc, license_raw) if license_raw else base_desc

    return jsonify({
        "latest_file": latest["filename"] if latest else None,
        "latest_url": "/latest" if latest else None,
        "broadcast_id": broadcast_id or None,
        "title": yt_title,
        "license": license_raw,
        "description": yt_desc,
    }), 200


@app.route("/youtube/update", methods=["GET", "POST"])
def youtube_update():
    params = _extract_params()
    latest, _ = _get_active_latest()
    if not latest:
        return jsonify({"error": "No '/latest' thumbnail available to upload"}), 404

    title_raw = params.get("title") if params.get("title") is not None else latest.get("title", "")
    subtitle_raw = params.get("subtitle") if params.get("subtitle") is not None else latest.get("subtitle", "")
    license_raw = params.get("license") if params.get("license") is not None else latest.get("attribution", "")
    broadcast_id = (params.get("broadcast_id") or cfg["youtube"]["default_broadcast_id"] or "").strip() or None
    description_override = params.get("description")

    yt_title = format_livestream_title(title_raw, subtitle_raw)
    thumbnail_path = os.path.join(cfg["output_dir"], latest["filename"])

    try:
        result = yt_service.update_livestream(
            broadcast_id=broadcast_id,
            title=yt_title,
            new_license=license_raw,
            description_override=description_override,
            thumbnail_path=thumbnail_path,
        )
        result["latest_file"] = latest["filename"]
        result["license"] = license_raw
        return jsonify(result), 200
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("YouTube update failed: %s", e)
        return jsonify({"error": str(e)}), 500


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
