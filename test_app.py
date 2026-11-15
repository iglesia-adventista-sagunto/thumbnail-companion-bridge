import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

# Isolate test outputs in a temporary directory before importing app
_TEMP_DIR = tempfile.TemporaryDirectory(prefix="thumb_test_", ignore_cleanup_errors=True)
_TEMP_CONFIG = os.path.join(_TEMP_DIR.name, "config.json")
shutil.copyfile(os.path.join(os.path.dirname(__file__), "config.template.json"), _TEMP_CONFIG)

os.environ["OUTPUT_DIR"] = _TEMP_DIR.name
os.environ["ATTRIBUTIONS_FILE"] = os.path.join(_TEMP_DIR.name, "attributions.txt")
os.environ["CONFIG_PATH"] = _TEMP_CONFIG
os.environ["CSV_PATH"] = os.path.join(_TEMP_DIR.name, "miniaturas.csv")

import app as bridge_app  # noqa: E402
import script as engine  # noqa: E402
from youtube_service import format_livestream_title, update_description_thumbnail_license  # noqa: E402


def _fake_variations(keyword_or_url, count=1, access_key=None):
    """Deterministic offline background generator for fast, reliable tests."""
    results = []
    for i in range(count):
        img = Image.new("RGBA", (1920, 1080), (30 + i * 10, 60, 100, 255))
        attr = {
            "author": "Test Photographer",
            "photo_url": f"https://example.com/photo-{i + 1}",
            "attribution_text": f"Photo by Test Photographer (mock {keyword_or_url} #{i + 1})",
        }
        results.append((img, attr))
    return results


class TestAutomaticThumbnailBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = bridge_app.app.test_client()
        cls.patcher = patch.object(engine, "get_unsplash_variations", side_effect=_fake_variations)
        cls.patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        _TEMP_DIR.cleanup()

    def test_01_health_endpoint(self):
        """GET /health returns 200 with service metadata."""
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "automatic-thumbnail")
        self.assertGreaterEqual(data["presets_count"], 1)

    def test_02_status_and_plaintext_endpoints(self):
        """GET /status, /is_busy, and /is_ready return expected formats."""
        res_status = self.client.get("/status")
        self.assertEqual(res_status.status_code, 200)
        self.assertIn("state", json.loads(res_status.data))

        res_busy = self.client.get("/is_busy")
        self.assertEqual(res_busy.status_code, 200)
        self.assertIn(res_busy.data.decode().strip(), ["true", "false"])

        res_ready = self.client.get("/is_ready")
        self.assertEqual(res_ready.status_code, 200)
        self.assertIn(res_ready.data.decode().strip(), ["true", "false"])

    def test_03_presets_endpoint(self):
        """GET /presets lists configured livestreaming presets."""
        res = self.client.get("/presets")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        names = [p["name"] for p in data["presets"]]
        self.assertIn("sabado", names)

    def test_04_generate_sync_post_and_get(self):
        """POST /generate and GET /generate render 1920x1080 PNG thumbnails."""
        res = self.client.post(
            "/generate",
            json={
                "title": "Culto Especial de Prueba",
                "subtitle": "Transmisión en Vivo",
                "query": "iglesia",
                "filename": "test_sync",
                "variations": 2,
            },
        )
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["files"]), 2)
        for fname in data["files"]:
            full_path = os.path.join(_TEMP_DIR.name, fname)
            self.assertTrue(os.path.isfile(full_path))
            with Image.open(full_path) as im:
                self.assertEqual(im.size, (1920, 1080))

        # Also verify GET /generate for Companion generic-http compatibility
        res_get = self.client.get("/generate?title=Prueba+GET&filename=test_get")
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(json.loads(res_get.data)["files"], ["test_get.png"])

    def test_05_preset_generate_with_override(self):
        """GET /<preset>/generate uses preset defaults and allows query overrides."""
        res = self.client.get("/sabado/generate?subtitle=Sermón+Especial")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data["preset"], "sabado")
        self.assertEqual(data["subtitle"], "Sermón Especial")
        self.assertEqual(data["files"], ["miniatura_sabado.png"])

    def test_06_generate_async(self):
        """POST /generate with async=true returns 202 and completes in background."""
        res = self.client.post(
            "/generate",
            json={
                "title": "Async Render",
                "filename": "test_async",
                "async": True,
            },
        )
        self.assertEqual(res.status_code, 202)
        for _ in range(30):
            if self.client.get("/is_busy").data.decode().strip() == "false":
                break
            time.sleep(0.05)
        self.assertEqual(self.client.get("/is_ready").data.decode().strip(), "true")
        self.assertTrue(os.path.isfile(os.path.join(_TEMP_DIR.name, "test_async.png")))

    def test_07_thumbnails_latest_and_delete(self):
        """GET /thumbnails, /thumbnails/<file>, /latest, and DELETE work safely."""
        res_list = self.client.get("/thumbnails")
        self.assertEqual(res_list.status_code, 200)
        thumbs = json.loads(res_list.data)["thumbnails"]
        self.assertGreaterEqual(len(thumbs), 1)

        res_latest = self.client.get("/latest")
        self.assertEqual(res_latest.status_code, 200)
        self.assertEqual(res_latest.mimetype, "image/png")

        res_latest_json = self.client.get("/latest?format=json")
        self.assertEqual(res_latest_json.status_code, 200)
        latest_name = json.loads(res_latest_json.data)["filename"]

        res_img = self.client.get(f"/thumbnails/{latest_name}")
        self.assertEqual(res_img.status_code, 200)
        self.assertEqual(res_img.mimetype, "image/png")

        # Path traversal protection
        res_bad = self.client.get("/thumbnails/..%2Fapp.py")
        self.assertIn(res_bad.status_code, [400, 404])

        # Delete
        res_del = self.client.delete("/thumbnails/test_get.png")
        self.assertEqual(res_del.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(_TEMP_DIR.name, "test_get.png")))

    def test_08_attributions_and_batch(self):
        """GET /attributions and POST /batch work end-to-end."""
        csv_body = "Filename,Title,Subtitle,Unsplash_Keyword_or_URL,Variations\nbatch_1,Culto Batch,Subtitulo,biblia,1\n"
        res_batch = self.client.post("/batch", json={"csv_content": csv_body})
        self.assertEqual(res_batch.status_code, 200)
        data = json.loads(res_batch.data)
        self.assertEqual(data["rows_processed"], 1)
        self.assertEqual(data["files"], ["batch_1.png"])

        res_attr = self.client.get("/attributions")
        self.assertEqual(res_attr.status_code, 200)
        self.assertIn("batch_1.png", res_attr.data.decode("utf-8"))

    def test_09_error_cases(self):
        """Invalid requests return clean 4xx JSON errors."""
        self.assertEqual(self.client.post("/generate", json={}).status_code, 400)
        self.assertEqual(self.client.get("/nonexistent_preset/generate").status_code, 404)
        self.assertEqual(self.client.get("/generate?title=Hi&variations=abc").status_code, 400)

    def test_10_web_ui_endpoint(self):
        """GET /ui and GET / with Accept: text/html serve the minimal Web UI."""
        res_ui = self.client.get("/ui")
        self.assertEqual(res_ui.status_code, 200)
        self.assertIn(b"Automatic Thumbnail Bridge", res_ui.data)

        res_html = self.client.get("/", headers={"Accept": "text/html"})
        self.assertEqual(res_html.status_code, 200)
        self.assertIn(b"Automatic Thumbnail Bridge", res_html.data)

        res_json = self.client.get("/", headers={"Accept": "application/json"})
        self.assertEqual(res_json.status_code, 200)
        self.assertEqual(json.loads(res_json.data)["status"], "healthy")

    def test_11_pin_latest_selection(self):
        """POST /latest pins a chosen image as the active /latest."""
        self.client.post("/generate", json={"title": "Primero", "filename": "pin_a"})
        self.client.post("/generate", json={"title": "Segundo", "filename": "pin_b"})

        res_pin = self.client.post("/latest", json={"filename": "pin_a.png"})
        self.assertEqual(res_pin.status_code, 200)
        data = json.loads(res_pin.data)
        self.assertEqual(data["latest"], "pin_a.png")
        self.assertTrue(data["pinned"])

        res_json = self.client.get("/latest?format=json")
        latest = json.loads(res_json.data)
        self.assertEqual(latest["filename"], "pin_a.png")
        self.assertEqual(latest["title"], "Primero")

        thumbs = json.loads(self.client.get("/thumbnails").data)["thumbnails"]
        flagged = [t["filename"] for t in thumbs if t["is_latest"]]
        self.assertEqual(flagged, ["pin_a.png"])

    def test_12_presets_crud(self):
        """POST /presets and DELETE /presets/<name> manage presets and persist config."""
        res_add = self.client.post(
            "/presets",
            json={
                "name": "jovenes",
                "title": "Culto de Jóvenes",
                "subtitle": "Viernes Noche",
                "query": "iglesia",
                "filename": "miniatura_jovenes",
                "variations": 1,
            },
        )
        self.assertEqual(res_add.status_code, 200)

        presets = json.loads(self.client.get("/presets").data)["presets"]
        self.assertTrue(any(p["name"] == "jovenes" for p in presets))

        res_gen = self.client.get("/jovenes/generate")
        self.assertEqual(res_gen.status_code, 200)
        self.assertEqual(json.loads(res_gen.data)["files"], ["miniatura_jovenes.png"])

        res_del = self.client.delete("/presets/jovenes")
        self.assertEqual(res_del.status_code, 200)
        presets_after = json.loads(self.client.get("/presets").data)["presets"]
        self.assertFalse(any(p["name"] == "jovenes" for p in presets_after))

    def test_13_csv_crud_and_run(self):
        """GET/POST /csv reads, writes, and optionally executes batch generation."""
        rows = [
            {
                "Filename": "csv_test_1",
                "Title": "Reunión General",
                "Subtitle": "Domingo",
                "Unsplash_Keyword_or_URL": "iglesia",
                "Variations": "1",
            }
        ]
        res_save = self.client.post("/csv", json={"rows": rows, "run": True})
        self.assertEqual(res_save.status_code, 200)
        save_data = json.loads(res_save.data)
        self.assertEqual(save_data["status"], "saved_and_generated")
        self.assertEqual(save_data["files"], ["csv_test_1.png"])

        res_get = self.client.get("/csv")
        self.assertEqual(res_get.status_code, 200)
        get_data = json.loads(res_get.data)
        self.assertEqual(len(get_data["rows"]), 1)
        self.assertEqual(get_data["rows"][0]["Title"], "Reunión General")

    def test_14_youtube_formatting_and_preview(self):
        """YouTube helpers format '[title] - [subtitle]' and update only miniatura license."""
        self.assertEqual(format_livestream_title("Culto", "Sábado"), "Culto - Sábado")
        self.assertEqual(format_livestream_title("Culto", ""), "Culto")

        desc = (
            "Bienvenidos al culto.\n\n"
            "Licencias:\n"
            "Música: Epidemic Sound\n"
            "Miniatura: Old Photo License\n"
            "Video: Propio"
        )
        updated = update_description_thumbnail_license(desc, "Photo by New Author on Unsplash")
        self.assertIn("Música: Epidemic Sound", updated)
        self.assertIn("Video: Propio", updated)
        self.assertIn("Miniatura: Photo by New Author on Unsplash", updated)
        self.assertNotIn("Old Photo License", updated)

        # Preview endpoint uses /latest metadata
        self.client.post("/generate", json={"title": "Sermón", "subtitle": "En Vivo", "filename": "yt_latest"})
        res_prev = self.client.post("/youtube/preview", json={"description": desc})
        self.assertEqual(res_prev.status_code, 200)
        prev = json.loads(res_prev.data)
        self.assertEqual(prev["latest_file"], "yt_latest.png")
        self.assertEqual(prev["title"], "Sermón - En Vivo")
        self.assertIn("Miniatura: Photo by Test Photographer", prev["description"])
        self.assertIn("Música: Epidemic Sound", prev["description"])

        # Update endpoint calls YouTubeService.update_livestream with /latest path
        with patch.object(
            bridge_app.yt_service,
            "update_livestream",
            return_value={"status": "ok", "broadcast_id": "abc123", "title": "Sermón - En Vivo", "thumbnail_updated": True},
        ) as mock_yt:
            res_upd = self.client.post("/youtube/update", json={"broadcast_id": "abc123"})
            self.assertEqual(res_upd.status_code, 200)
            mock_yt.assert_called_once()
            _, kwargs = mock_yt.call_args
            self.assertEqual(kwargs["broadcast_id"], "abc123")
            self.assertEqual(kwargs["title"], "Sermón - En Vivo")
            self.assertTrue(kwargs["thumbnail_path"].endswith("yt_latest.png"))


if __name__ == "__main__":
    unittest.main()
