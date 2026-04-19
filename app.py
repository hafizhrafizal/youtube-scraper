import json
import io
import re
import base64

from flask import Flask, render_template, request, Response, jsonify, stream_with_context
import googleapiclient.discovery
from googleapiclient.errors import HttpError
import pandas as pd

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(title):
    safe = re.sub(r'[^\x20-\x7e]', '_', title)   # strip non-printable-ASCII (fixes latin-1 header errors)
    safe = re.sub(r'[<>:"/\\|?*]', '_', safe)
    safe = re.sub(r'\s+', ' ', safe).strip('. ')
    return safe[:120] or "video"


def _get_video_metadata(youtube, video_id):
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


# ---------------------------------------------------------------------------
# Scrape generator — yields SSE strings directly, no threads needed
# ---------------------------------------------------------------------------

def _scrape_generator(api_key, video_ids):
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
                metadata = _get_video_metadata(youtube, video_id)
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

                while True:
                    page += 1
                    try:
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

                        if snippet.get("totalReplyCount", 0) > 0:
                            replies = _get_replies(youtube, item["id"], video_id, metadata)
                            comments.extend(replies)

                    yield sse({"type": "progress", "video_index": idx,
                               "message": f"  Page {page} done — {len(comments):,} comments so far"})

                    next_page_token = response.get("nextPageToken")
                    if not next_page_token:
                        break

                filename = f"comments_{_safe_filename(title)}.csv"
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

                df       = pd.DataFrame(comments)
                buf      = io.StringIO()
                df.to_csv(buf, index=False)
                csv_b64  = base64.b64encode(buf.getvalue().encode("utf-8")).decode("utf-8")

                yield sse({
                    "type":        "video_done",
                    "video_index": idx,
                    "filename":    filename,
                    "message":     f"Done — {len(df):,} rows collected.",
                    "csv":         csv_b64,
                })

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

    return Response(
        stream_with_context(_scrape_generator(api_key, video_ids)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
