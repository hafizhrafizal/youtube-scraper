import json
import io
import os
import re
import base64
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, request, Response, jsonify, stream_with_context
import googleapiclient.discovery
from googleapiclient.errors import HttpError
import pandas as pd

app = Flask(__name__)

DEFAULT_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")


# ---------------------------------------------------------------------------
# Usage limits
#
# The shared default key has a 10,000 unit/day YouTube quota (1 unit = one
# page of up to 100 comments or replies). Usage of that key is metered per IP
# and globally so one visitor can't exhaust it for everyone. Visitors using
# their own key are only subject to the request-rate / concurrency limits.
# ---------------------------------------------------------------------------

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

IP_DAILY_UNITS     = _env_int("LIMIT_IP_DAILY_UNITS", 2000)    # ~200k comments per IP per day
GLOBAL_DAILY_UNITS = _env_int("LIMIT_GLOBAL_DAILY_UNITS", 9500) # headroom under the 10k quota
MAX_VIDEOS         = _env_int("LIMIT_MAX_VIDEOS", 25)           # per scrape request
SCRAPES_PER_HOUR   = _env_int("LIMIT_SCRAPES_PER_HOUR", 40)     # per IP
MAX_CONCURRENT     = _env_int("LIMIT_MAX_CONCURRENT", 2)        # simultaneous scrapes per IP
VALIDATES_PER_HOUR = _env_int("LIMIT_VALIDATES_PER_HOUR", 30)   # per IP

USAGE_DB = os.environ.get("USAGE_DB", os.path.join(os.path.dirname(__file__), "data", "usage.db"))

try:
    from zoneinfo import ZoneInfo
    _QUOTA_TZ = ZoneInfo("America/Los_Angeles")   # YouTube quota resets at midnight Pacific
except Exception:
    _QUOTA_TZ = timezone(timedelta(hours=-8))


class QuotaExceeded(Exception):
    pass


os.makedirs(os.path.dirname(USAGE_DB), exist_ok=True)
_db = sqlite3.connect(USAGE_DB, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, ip TEXT, units INTEGER, PRIMARY KEY (day, ip))")
_db.commit()
_db_lock = threading.Lock()

# In-memory counters; gunicorn runs a single (threaded) worker so these are shared.
_mem_lock = threading.Lock()
_recent   = defaultdict(lambda: defaultdict(deque))   # kind -> ip -> timestamps
_active   = defaultdict(int)                          # ip -> running scrapes


def _client_ip():
    # Container is only reachable through host nginx, which sets X-Real-IP.
    return request.headers.get("X-Real-IP") or request.remote_addr or "unknown"


def _quota_day():
    return datetime.now(_QUOTA_TZ).strftime("%Y-%m-%d")


def _usage(ip):
    day = _quota_day()
    with _db_lock:
        row = _db.execute("SELECT units FROM usage WHERE day=? AND ip=?", (day, ip)).fetchone()
        total = _db.execute("SELECT COALESCE(SUM(units), 0) FROM usage WHERE day=?", (day,)).fetchone()[0]
    return (row[0] if row else 0), total


def _charge_default_key(ip):
    """Return a callable that records 1 quota unit, raising QuotaExceeded when over a limit."""
    def charge():
        day = _quota_day()
        with _db_lock:
            row  = _db.execute("SELECT units FROM usage WHERE day=? AND ip=?", (day, ip)).fetchone()
            used = row[0] if row else 0
            total = _db.execute("SELECT COALESCE(SUM(units), 0) FROM usage WHERE day=?", (day,)).fetchone()[0]
            if used >= IP_DAILY_UNITS:
                raise QuotaExceeded(
                    f"Daily limit for the shared API key reached ({IP_DAILY_UNITS:,} units). "
                    "It resets at midnight Pacific time, or add your own API key to keep going.")
            if total >= GLOBAL_DAILY_UNITS:
                raise QuotaExceeded(
                    "The shared API key is out of quota for today. "
                    "It resets at midnight Pacific time, or add your own API key to keep going.")
            _db.execute(
                "INSERT INTO usage (day, ip, units) VALUES (?, ?, 1) "
                "ON CONFLICT(day, ip) DO UPDATE SET units = units + 1",
                (day, ip))
            # Opportunistically drop old days
            _db.execute("DELETE FROM usage WHERE day < ?", ((datetime.now(_QUOTA_TZ) - timedelta(days=3)).strftime("%Y-%m-%d"),))
            _db.commit()
    return charge


def _no_charge():
    pass


def _hit_rate_limit(kind, ip, limit, window=3600):
    """Record a request; return seconds to wait if over `limit` per `window`, else 0."""
    now = time.time()
    with _mem_lock:
        q = _recent[kind][ip]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return int(q[0] + window - now) + 1
        q.append(now)
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(title):
    safe = re.sub(r'[^\x20-\x7e]', '_', title)   # strip non-printable-ASCII (fixes latin-1 header errors)
    safe = re.sub(r'[<>:"/\\|?*]', '_', safe)
    safe = re.sub(r'\s+', ' ', safe).strip('. ')
    return safe[:120] or "video"


def _get_video_metadata(youtube, video_id, charge):
    charge()
    response = youtube.videos().list(
        part="snippet,statistics,contentDetails,status,recordingDetails,topicDetails",
        id=video_id
    ).execute()

    if not response.get("items"):
        return None

    video      = response["items"][0]
    snippet    = video.get("snippet", {})
    statistics = video.get("statistics", {})
    topics     = video.get("topicDetails", {})

    return {
        "video_id":          video_id,
        "video_title":       snippet.get("title"),
        "video_description": snippet.get("description"),
        "video_tags":        ", ".join(snippet.get("tags", [])),
        "video_publishedAt": snippet.get("publishedAt"),
        "channel_id":        snippet.get("channelId"),
        "channel_title":     snippet.get("channelTitle"),
        "category_id":       snippet.get("categoryId"),
        "view_count":        int(statistics.get("viewCount", 0)),
        "like_count":        int(statistics.get("likeCount", 0)),
        "favorite_count":    int(statistics.get("favoriteCount", 0)),
        "comment_count":     int(statistics.get("commentCount", 0)),
        "topic_categories":  ", ".join(topics.get("topicCategories", [])),
    }


def _get_replies(youtube, parent_id, video_id, metadata, charge):
    replies = []
    next_page_token = None

    while True:
        charge()
        reply_response = youtube.comments().list(
            part="snippet",
            parentId=parent_id,
            maxResults=100,
            pageToken=next_page_token,
            textFormat="plainText"
        ).execute()

        for reply in reply_response.get("items", []):
            s = reply["snippet"]
            replies.append({
                "comment_id":   reply["id"],
                "username":     s.get("authorDisplayName"),
                "user_id":      s.get("authorChannelId", {}).get("value"),
                "comment_date": s.get("publishedAt"),
                "rawContent":   s.get("textOriginal"),
                "like_count":   s.get("likeCount", 0),
                "reply_count":  0,
                "parent_id":    s.get("parentId"),
                "video_url":    f"https://www.youtube.com/watch?v={video_id}",
                **metadata,
            })

        next_page_token = reply_response.get("nextPageToken")
        if not next_page_token:
            break

    return replies


def _to_csv_b64(rows):
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return base64.b64encode(buf.getvalue().encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# Scrape generator — yields SSE strings directly, no threads needed
# ---------------------------------------------------------------------------

def _scrape_generator(api_key, video_ids, charge=_no_charge):
    """
    Generator that streams SSE-formatted progress events while scraping.
    Each video_done event carries the CSV encoded as base64 so the browser
    can download it without a separate /download endpoint (Vercel-compatible).
    """

    def sse(d):
        return f"data: {json.dumps(d)}\n\n"

    try:
        youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=api_key)
        total   = len(video_ids)
        statuses = ["pending"] * total

        for idx, video_id in enumerate(video_ids):
            yield sse({"type": "video_start", "video_index": idx, "video_id": video_id})
            statuses[idx] = "running"

            try:
                metadata = _get_video_metadata(youtube, video_id, charge)
                if not metadata:
                    statuses[idx] = "error"
                    yield sse({"type": "video_error", "video_index": idx,
                               "message": "Video not found or unavailable."})
                    continue

                title = metadata["video_title"]
                yield sse({"type": "video_title", "video_index": idx, "title": title})
                yield sse({"type": "progress", "video_index": idx,
                           "message": f'Found: "{title}" — {metadata["comment_count"]:,} comments expected'})

                comments = []
                next_page_token = None
                page = 0
                comments_disabled = False
                quota_msg = None

                while True:
                    page += 1
                    try:
                        charge()
                        response = youtube.commentThreads().list(
                            part="snippet",
                            videoId=video_id,
                            maxResults=100,
                            pageToken=next_page_token,
                            textFormat="plainText"
                        ).execute()
                    except HttpError as exc:
                        reason = ""
                        try:
                            reason = json.loads(exc.content)["error"]["errors"][0]["reason"]
                        except Exception:
                            pass
                        if reason == "commentsDisabled":
                            comments_disabled = True
                            break
                        raise
                    except QuotaExceeded as exc:
                        quota_msg = str(exc)
                        break

                    for item in response.get("items", []):
                        snippet = item["snippet"]
                        top     = snippet["topLevelComment"]["snippet"]

                        comments.append({
                            "comment_id":   item["id"],
                            "username":     top.get("authorDisplayName"),
                            "user_id":      top.get("authorChannelId", {}).get("value"),
                            "comment_date": top.get("publishedAt"),
                            "rawContent":   top.get("textOriginal"),
                            "like_count":   top.get("likeCount", 0),
                            "reply_count":  snippet.get("totalReplyCount", 0),
                            "parent_id":    None,
                            "video_url":    f"https://www.youtube.com/watch?v={video_id}",
                            **metadata,
                        })

                        if snippet.get("totalReplyCount", 0) > 0 and not quota_msg:
                            try:
                                comments.extend(_get_replies(youtube, item["id"], video_id, metadata, charge))
                            except QuotaExceeded as exc:
                                quota_msg = str(exc)

                    yield sse({"type": "progress", "video_index": idx,
                               "message": f"  Page {page} done — {len(comments):,} comments so far"})

                    next_page_token = response.get("nextPageToken")
                    if not next_page_token or quota_msg:
                        break

                filename = f"comments_{_safe_filename(title)}.csv"

                if quota_msg:
                    if comments:
                        statuses[idx] = "done"
                        yield sse({
                            "type":        "video_done",
                            "video_index": idx,
                            "filename":    filename,
                            "message":     f"Stopped early — {len(comments):,} rows collected (partial). {quota_msg}",
                            "csv":         _to_csv_b64(comments),
                        })
                    else:
                        statuses[idx] = "error"
                        yield sse({"type": "video_error", "video_index": idx, "message": quota_msg})
                    raise QuotaExceeded(quota_msg)

                statuses[idx] = "done"

                if comments_disabled:
                    yield sse({
                        "type":        "video_done",
                        "video_index": idx,
                        "filename":    filename,
                        "message":     "Comments are disabled for this video.",
                        "csv":         None,
                    })
                    continue

                if not comments:
                    yield sse({
                        "type":        "video_done",
                        "video_index": idx,
                        "filename":    filename,
                        "message":     "This video has no comments yet.",
                        "csv":         None,
                    })
                    continue

                yield sse({
                    "type":        "video_done",
                    "video_index": idx,
                    "filename":    filename,
                    "message":     f"Done — {len(comments):,} rows collected.",
                    "csv":         _to_csv_b64(comments),
                })

            except QuotaExceeded as exc:
                if statuses[idx] == "running":
                    statuses[idx] = "error"
                    yield sse({"type": "video_error", "video_index": idx, "message": str(exc)})
                for rest in range(idx + 1, total):
                    statuses[rest] = "error"
                    yield sse({"type": "video_error", "video_index": rest,
                               "message": "Skipped — usage limit reached."})
                break

            except Exception as exc:
                statuses[idx] = "error"
                yield sse({"type": "video_error", "video_index": idx, "message": str(exc)})

        done   = statuses.count("done")
        errors = total - done
        yield sse({
            "type":    "all_done",
            "message": f"All {total} video(s) processed — {done} succeeded, {errors} failed.",
        })

    except Exception as exc:
        yield sse({"type": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config():
    cfg = {"has_default_key": bool(DEFAULT_API_KEY), "max_videos": MAX_VIDEOS}
    if DEFAULT_API_KEY:
        used, total = _usage(_client_ip())
        cfg["usage"] = {
            "used":         used,
            "limit":        IP_DAILY_UNITS,
            "shared_left":  max(0, GLOBAL_DAILY_UNITS - total),
        }
    return jsonify(cfg)


@app.route("/validate-key", methods=["POST"])
def validate_key():
    wait = _hit_rate_limit("validate", _client_ip(), VALIDATES_PER_HOUR)
    if wait:
        return jsonify({"valid": False, "message": f"Too many attempts. Try again in {wait // 60 + 1} min."}), 429
    api_key = (request.json or {}).get("api_key", "").strip()
    if not api_key:
        return jsonify({"valid": False, "message": "API key cannot be empty."})
    try:
        yt = googleapiclient.discovery.build("youtube", "v3", developerKey=api_key)
        yt.videos().list(part="snippet", id="dQw4w9WgXcQ").execute()
        return jsonify({"valid": True})
    except Exception as exc:
        return jsonify({"valid": False, "message": str(exc)})


@app.route("/scrape", methods=["POST"])
def start_scrape():
    data      = request.json or {}
    user_key  = data.get("api_key", "").strip()
    api_key   = user_key or DEFAULT_API_KEY
    video_ids = data.get("video_ids", [])
    ip        = _client_ip()

    if not api_key:
        return jsonify({"error": "API key is required."}), 400
    if not video_ids:
        return jsonify({"error": "At least one video URL is required."}), 400
    if len(video_ids) > MAX_VIDEOS:
        return jsonify({"error": f"Too many videos — at most {MAX_VIDEOS} per run."}), 400

    charge = _no_charge
    if not user_key:
        used, total = _usage(ip)
        if used >= IP_DAILY_UNITS or total >= GLOBAL_DAILY_UNITS:
            return jsonify({"error": "Daily limit for the shared API key reached. It resets at "
                                     "midnight Pacific time, or add your own API key to keep going."}), 429
        charge = _charge_default_key(ip)

    with _mem_lock:
        if _active.get(ip, 0) >= MAX_CONCURRENT:
            return jsonify({"error": f"You already have {MAX_CONCURRENT} scrapes running. "
                                     "Wait for one to finish."}), 429
    wait = _hit_rate_limit("scrape", ip, SCRAPES_PER_HOUR)
    if wait:
        return jsonify({"error": f"Too many scrape requests. Try again in {wait // 60 + 1} min."}), 429

    with _mem_lock:
        _active[ip] += 1

    def release():
        with _mem_lock:
            _active[ip] -= 1
            if _active[ip] <= 0:
                del _active[ip]

    resp = Response(
        stream_with_context(_scrape_generator(api_key, video_ids, charge)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    resp.call_on_close(release)
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
