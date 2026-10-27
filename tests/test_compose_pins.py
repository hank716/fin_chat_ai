"""spec 027：cloudflared 必須釘明確版本，避免 :latest 靜默落後（曾卡在 2026.5.2 五個月）。"""
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def _image(service: str) -> str:
    if not COMPOSE.exists():
        pytest.skip("docker-compose.yml 不在此環境（例如 backend 容器內）")
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data["services"][service]["image"]


def test_cloudflared_image_is_pinned():
    image = _image("cloudflared")
    assert ":" in image, f"cloudflared 沒有版本 tag：{image}"
    tag = image.rsplit(":", 1)[1]
    assert tag != "latest", "cloudflared 不可用 :latest，請釘明確版本（見 DEPLOY.md §7.1）"
    assert tag[0].isdigit(), f"cloudflared tag 應為版本號：{tag}"
