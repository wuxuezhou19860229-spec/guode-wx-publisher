"""微信云托管发布服务

用途：接收一篇已排版好的公众号稿件（正文 HTML + 封面图 + 正文配图），
依次完成：上传正文图片 -> 上传封面永久素材 -> 新建草稿 -> 提交发布。

必须在「微信云托管」容器内运行：它依赖云托管的「开放接口服务」。
该服务会在容器内旁加载一个代理，中转 api.weixin.qq.com 的请求，
自动处理 access_token 与鉴权，因此本服务：
  1) 请求里不带 access_token；
  2) 公众号后台不需要启用 AppSecret、不需要配置 IP 白名单。
前提是在「微信云托管控制台 - 云调用 - 微信令牌权限」中已登记下列接口路径：
  /cgi-bin/media/uploadimg
  /cgi-bin/material/add_material
  /cgi-bin/draft/add
  /cgi-bin/freepublish/submit
  /cgi-bin/freepublish/get

环境变量：
  AUTH_TOKEN    必填，调用方需在请求头 X-Auth-Token 中带上同一个值
  DEFAULT_AUTHOR 选填，默认作者名
  WX_API_BASE   选填，默认 https://api.weixin.qq.com
  PORT          选填，默认 8080
"""

import json
import logging
import os
import re
import uuid

import requests
from flask import Flask, jsonify, request

APP = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("wx-publisher")

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
DEFAULT_AUTHOR = os.environ.get("DEFAULT_AUTHOR", "国德招采数智服务平台")
WX_API_BASE = os.environ.get("WX_API_BASE", "http://api.weixin.qq.com").rstrip("/")
HTTP_TIMEOUT = 30

MAX_TITLE = 64
MAX_DIGEST = 120


class WxError(RuntimeError):
    def __init__(self, step, payload):
        self.step = step
        self.payload = payload
        super().__init__(f"{step} failed: {payload}")


def _wx_post(path, *, params=None, data=None, files=None, raw_body=None):
    """调用微信接口。

    注意：必须走 http。云托管的「开放接口服务」在容器内是本地代理，
    https 会命中自签名证书导致 CERTIFICATE_VERIFY_FAILED；官方文档也说明
    云调用"不需使用 HTTPS 协议"。
    """
    url = WX_API_BASE + path
    headers = {}
    if raw_body is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.post(
        url,
        params=params,
        data=raw_body if raw_body is not None else data,
        files=files,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    try:
        payload = resp.json()
    except ValueError:
        raise WxError(path, {"status": resp.status_code, "body": resp.text[:400]})
    if payload.get("errcode"):
        raise WxError(path, payload)
    return payload


def _upload_inline_image(stream, filename):
    """正文配图：上传到微信，返回可在正文里引用的 URL。"""
    payload = _wx_post(
        "/cgi-bin/media/uploadimg",
        files={"media": (filename, stream, "image/jpeg")},
    )
    url = payload.get("url")
    if not url:
        raise WxError("/cgi-bin/media/uploadimg", payload)
    return url


def _upload_cover(stream, filename):
    """封面图：作为永久图片素材上传，返回 media_id 供草稿的 thumb_media_id 使用。"""
    payload = _wx_post(
        "/cgi-bin/material/add_material",
        params={"type": "image"},
        files={"media": (filename, stream, "image/jpeg")},
    )
    media_id = payload.get("media_id")
    if not media_id:
        raise WxError("/cgi-bin/material/add_material", payload)
    return media_id, payload.get("url")


def _add_draft(article):
    payload = _wx_post("/cgi-bin/draft/add", raw_body=json.dumps({"articles": [article]}, ensure_ascii=False).encode("utf-8"))
    media_id = payload.get("media_id")
    if not media_id:
        raise WxError("/cgi-bin/draft/add", payload)
    return media_id


def _submit_publish(media_id):
    payload = _wx_post("/cgi-bin/freepublish/submit", raw_body=json.dumps({"media_id": media_id}).encode("utf-8"))
    return payload.get("publish_id"), payload


def _query_publish(publish_id):
    try:
        payload = _wx_post(
            "/cgi-bin/freepublish/get",
            raw_body=json.dumps({"publish_id": publish_id}).encode("utf-8"),
        )
        return payload
    except WxError as exc:
        # 查询失败不影响已提交的发布，仅记录
        LOG.warning("poll publish status failed: %s", exc.payload)
        return None


def _clean(html):
    """去掉脚本与事件属性，避免微信侧过滤或截断。"""
    html = re.sub(r"(?is)<script.*?</script>", "", html)
    html = re.sub(r'(?i)\son\w+\s*=\s*"[^"]*"', "", html)
    return html.strip()


def _require_auth():
    if not AUTH_TOKEN:
        return "server AUTH_TOKEN not configured"
    if request.headers.get("X-Auth-Token", "") != AUTH_TOKEN:
        return "invalid token"
    return None


@APP.get("/healthz")
def healthz():
    return jsonify(ok=True, service="wx-cloudrun-publisher", api_base=WX_API_BASE)


@APP.post("/publish")
def publish():
    denied = _require_auth()
    if denied:
        return jsonify(ok=False, error=denied), 401

    try:
        meta = json.loads(request.form.get("meta", "{}"))
    except ValueError:
        return jsonify(ok=False, error="meta 不是合法 JSON"), 400

    title = (meta.get("title") or "").strip()
    content_html = _clean(meta.get("content_html") or "")
    if not title or not content_html:
        return jsonify(ok=False, error="缺少 title 或 content_html"), 400
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE]

    result = {"ok": False, "request_id": meta.get("request_id") or uuid.uuid4().hex, "steps": {}}

    try:
        # 1. 正文配图按顺序替换 {{img0}}、{{img1}} ...
        image_files = request.files.getlist("images")
        for index, storage in enumerate(image_files):
            url = _upload_inline_image(storage.stream, storage.filename or f"img{index}.jpg")
            content_html = content_html.replace("{{img%d}}" % index, url)
        if image_files:
            result["steps"]["inline_images"] = len(image_files)

        # 2. 封面图（缺省时取第一张正文图，仍失败则拒绝，避免草稿无法保存）
        cover = request.files.get("cover")
        if cover is not None:
            thumb_media_id, cover_url = _upload_cover(cover.stream, cover.filename or "cover.jpg")
            result["steps"]["cover_url"] = cover_url
        else:
            thumb_media_id, cover_url = _upload_cover(
                _first_inline_image(request), "cover.jpg"
            )
            result["steps"]["cover_url"] = cover_url
        result["steps"]["thumb_media_id"] = thumb_media_id

        # 3. 建草稿
        article = {
            "title": title,
            "author": (meta.get("author") or DEFAULT_AUTHOR)[:16],
            "digest": (meta.get("digest") or "")[:MAX_DIGEST],
            "content": content_html,
            "content_source_url": meta.get("content_source_url") or "",
            "thumb_media_id": thumb_media_id,
            "need_open_comment": int(meta.get("need_open_comment", 1)),
            "only_fans_can_comment": int(meta.get("only_fans_can_comment", 0)),
        }
        draft_media_id = _add_draft(article)
        result["steps"]["draft_media_id"] = draft_media_id
        result["ok"] = True

        # 4. 提交发布（auto_publish 为 false 时只停留在草稿箱）
        if meta.get("auto_publish"):
            publish_id, raw = _submit_publish(draft_media_id)
            result["steps"]["publish_id"] = publish_id
            result["publish"] = raw
            status = _query_publish(publish_id) if publish_id else None
            if status:
                result["publish_status"] = status

        return jsonify(result)

    except WxError as exc:
        LOG.error("failed at %s: %s", exc.step, exc.payload)
        result["ok"] = False
        result["error"] = str(exc)
        result["failed_step"] = exc.step
        result["detail"] = exc.payload
        return jsonify(result), 502

    except Exception as exc:  # noqa: BLE001
        LOG.exception("unexpected failure")
        result["ok"] = False
        result["error"] = repr(exc)
        return jsonify(result), 500


def _first_inline_image(req):
    """从正文图里取第一张作为封面兜底。"""
    images = req.files.getlist("images")
    if not images:
        raise WxError("/publish", {"error": "既没有 cover，也没有 images，无法生成封面"})
    storage = images[0]
    storage.stream.seek(0)
    return storage.stream


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
