import json
import io
import re
import uuid
import threading
import queue
import time

from flask import Flask, render_template, request, Response, jsonify
import googleapiclient.discovery
import pandas as pd

app = Flask(__name__)
# jobs[job_id] = {queue, status, videos: [{video_id, status, title, csv_data, filename}]}
jobs = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(title):
    """Strip filesystem-illegal characters and limit length."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)
    safe = re.sub(r'\s+', ' ', safe).strip('. ')
    return (safe[:120] or "video")


def _get_video_metadata(youtube, video_id):
    response = youtube.videos().list(
        part="snippet,statistics,contentDetails,status,recordingDetails,topicDetails",
        id=video_id
    ).execute()

    if not response.get("items"):
        return None

    video = response["items"][0]
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


def _get_replies(youtube, parent_id, video_id, metadata):
    replies = []
    next_page_token = None

    while True:
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


def _getcomments(youtube, video_id, q, video_index, video_info):
    """Fetch all comments for a video, emitting progress events tagged with video_index."""
    metadata = _get_video_metadata(youtube, video_id)
    if not metadata:
        return pd.DataFrame()

    title = metadata["video_title"]
    video_info["title"] = title  # stored so _run_scrape can build the filename

    q.put({"type": "video_title", "video_index": video_index, "title": title})
    q.put({
        "type": "progress",
        "video_index": video_index,
        "message": f'Found: "{title}" — {metadata["comment_count"]:,} comments expected',
    })

    comments = []
    next_page_token = None
    page = 0

    while True:
        page += 1
        response = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=100,
            pageToken=next_page_token,
            textFormat="plainText"
        ).execute()

        for item in response.get("items", []):
            snippet = item["snippet"]
            top = snippet["topLevelComment"]["snippet"]

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

            if snippet.get("totalReplyCount", 0) > 0:
                replies = _get_replies(youtube, item["id"], video_id, metadata)
                comments.extend(replies)

        q.put({
            "type": "progress",
            "video_index": video_index,
            "message": f"  Page {page} done — {len(comments):,} comments collected so far",
        })

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    return pd.DataFrame(comments)


# ---------------------------------------------------------------------------
# Background scrape worker
# ---------------------------------------------------------------------------

def _run_scrape(job_id, api_key, video_ids):
    job = jobs[job_id]
    q   = job["queue"]

    try:
        youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=api_key)
        total   = len(video_ids)

        for idx, video_id in enumerate(video_ids):
            video_info = job["videos"][idx]

            q.put({"type": "video_start", "video_index": idx, "video_id": video_id})
            video_info["status"] = "running"

            try:
                df = _getcomments(youtube, video_id, q, idx, video_info)

                buf = io.StringIO()
                df.to_csv(buf, index=False)
                csv_bytes = buf.getvalue().encode("utf-8")

                title    = video_info.get("title") or video_id
                filename = f"comments_{_safe_filename(title)}.csv"

                video_info["csv_data"] = csv_bytes
                video_info["filename"] = filename
                video_info["status"]   = "done"

                q.put({
                    "type":        "video_done",
                    "video_index": idx,
                    "job_id":      job_id,
                    "filename":    filename,
                    "message":     f"Done — {len(df):,} rows collected. Downloading {filename}…",
                })

            except Exception as exc:
                video_info["status"] = "error"
                q.put({"type": "video_error", "video_index": idx, "message": str(exc)})

        done_count  = sum(1 for v in job["videos"] if v["status"] == "done")
        error_count = total - done_count
        job["status"] = "done"
        q.put({
            "type":    "all_done",
            "message": (
                f"All {total} video(s) processed — "
                f"{done_count} succeeded, {error_count} failed."
            ),
        })

    except Exception as exc:
        job["status"] = "error"
        q.put({"type": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/validate-key", methods=["POST"])
def validate_key():
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
    api_key   = data.get("api_key", "").strip()
    video_ids = data.get("video_ids", [])

    if not api_key:
        return jsonify({"error": "API key is required."}), 400
    if not video_ids:
        return jsonify({"error": "At least one video URL is required."}), 400

    job_id = str(uuid.uuid4())
    q      = queue.Queue()
    jobs[job_id] = {
        "queue":  q,
        "status": "running",
        "videos": [
            {"video_id": vid, "status": "pending",
             "title": None, "csv_data": None, "filename": None}
            for vid in video_ids
        ],
    }

    t = threading.Thread(target=_run_scrape, args=(job_id, api_key, video_ids), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "total": len(video_ids)})


@app.route("/progress/<job_id>")
def progress(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found."}), 404

    def generate():
        job = jobs[job_id]
        while True:
            try:
                event = job["queue"].get(timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] in ("all_done", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/download/<job_id>/<int:video_index>")
def download(job_id, video_index):
    job = jobs.get(job_id)
    if not job:
        return "Job not found.", 404
    videos = job.get("videos", [])
    if video_index >= len(videos):
        return "Invalid video index.", 404
    v = videos[video_index]
    if v["status"] != "done" or not v["csv_data"]:
        return "Download not ready.", 404

    return Response(
        v["csv_data"],
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{v["filename"]}"'},
    )


if __name__ == "__main__":
    app.run(debug=True, threaded=True, port=5000)
