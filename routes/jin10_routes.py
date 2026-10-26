import json
import logging

from flask import Blueprint, Response, request

from routes.security import rate_limit
from routes.shared import _cfg
from services.jin10_feed import build_jin10_rss

logger = logging.getLogger(__name__)
jin10_bp = Blueprint("jin10", __name__)


@jin10_bp.route("/jin10/rss")
@rate_limit(max_requests=30, window_seconds=60)
def jin10_rss():
    """金十数据原始 RSS 源，供 AI-Filter 当普通 RSSHub 路由消费。"""
    cfg = _cfg()
    limit = request.args.get("limit", 50, type=int)
    limit = min(max(limit or 50, 1), 50)
    try:
        xml = build_jin10_rss(request.url, cfg, limit=limit)
        return Response(xml, mimetype="application/rss+xml")
    except Exception as exc:
        logger.exception("[Jin10RSS] 构建 RSS 失败: %s", exc)
        return Response(
            json.dumps({"error": str(exc)}, ensure_ascii=False),
            status=502,
            mimetype="application/json",
        )
