#INSTALL REQUIRED PACKAGE
#!pip install google-api-python-client

import googleapiclient.discovery
import pandas as pd
from tqdm import tqdm

# --- YouTube API Setup ---
api_service_name = "youtube"
api_version = "v3"
#############CUSTOMIZE HERE - INSERT YOUR DEV KEY#######################
DEVELOPER_KEY = 'AIzaSyDbluwbTaRxlEOnVRiUUXdsrSEcL9krpOg' # Replace with your actual key
##################################################
youtube = googleapiclient.discovery.build(api_service_name, api_version, developerKey=DEVELOPER_KEY)

##################SCRAPING REQUIRED FUNCTION, CHANGE ONLY IF YOU NEED MORE METADATA TO SCRAPE###################
# --- Helper: Get Extended Metadata ---
def get_video_metadata(video_id):
    request = youtube.videos().list(
        part="snippet,statistics,contentDetails,status,recordingDetails,topicDetails",
        id=video_id
    )
    response = request.execute()
    if not response['items']:
        return None

    video = response['items'][0]
    snippet = video.get("snippet", {})
    statistics = video.get("statistics", {})
    content = video.get("contentDetails", {})
    status = video.get("status", {})

    topics = video.get("topicDetails", {})


    return {
        "video_id": video_id,
        "video_title": snippet.get("title"),
        "video_description": snippet.get("description"),
        "video_tags": ", ".join(snippet.get("tags", [])),
        "video_publishedAt": snippet.get("publishedAt"),
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "category_id": snippet.get("categoryId"),
        "view_count": int(statistics.get("viewCount", 0)),
        "like_count": int(statistics.get("likeCount", 0)),
        "favorite_count": int(statistics.get("favoriteCount", 0)),
        "comment_count": int(statistics.get("commentCount", 0)),
        "topic_categories": ", ".join(topics.get("topicCategories", [])),
    }


# --- Helper: Fetch Replies ---
def get_replies(parent_id, video_id, metadata):
    replies = []
    next_page_token = None

    while True:
        reply_request = youtube.comments().list(
            part="snippet",
            parentId=parent_id,
            maxResults=100,
            pageToken=next_page_token,
            textFormat="plainText"
        )
        reply_response = reply_request.execute()

        for reply in reply_response.get("items", []):
            s = reply["snippet"]
            replies.append({
                "comment_id": reply["id"],
                "username": s.get("authorDisplayName"),
                "user_id": s.get("authorChannelId", {}).get("value"),
                "comment_date": s.get("publishedAt"),
                "rawContent": s.get("textOriginal"),
                "like_count": s.get("likeCount", 0),
                "reply_count": 0,
                "parent_id": s.get("parentId"),
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
                **metadata
            })

        next_page_token = reply_response.get("nextPageToken")
        if not next_page_token:
            break

    return replies


# --- Main: Get Comments (top-level + replies) ---
def getcomments(video_id):
    metadata = get_video_metadata(video_id)
    if not metadata:
        return pd.DataFrame()  # Return empty if video not found or removed

    comments = []
    next_page_token = None

    while True:
        request = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=100,
            pageToken=next_page_token,
            textFormat="plainText"
        )
        response = request.execute()

        for item in response.get("items", []):
            snippet = item["snippet"]
            top_comment = snippet["topLevelComment"]["snippet"]

            comments.append({
                "comment_id": item["id"],
                "username": top_comment.get("authorDisplayName"),
                "user_id": top_comment.get("authorChannelId", {}).get("value"),
                "comment_date": top_comment.get("publishedAt"),
                "rawContent": top_comment.get("textOriginal"),
                "like_count": top_comment.get("likeCount", 0),
                "reply_count": snippet.get("totalReplyCount", 0),
                "parent_id": None,
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
                **metadata
            })

            # Get replies if available
            if snippet.get("totalReplyCount", 0) > 0:
                replies = get_replies(item["id"], video_id, metadata)
                comments.extend(replies)

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    return pd.DataFrame(comments)


from tqdm import tqdm
import pandas as pd

df = pd.DataFrame()

##Add All Video_ID to scrape (e.g https://www.youtube.com/watch?v=ERBVPGn_MM0 --> Video_id =ERBVPGn_MM0 )

video_id_list = ["OlXb7K_h8PM","oOwaNMfMx8M"]


# Loop through and collect
for video_id in tqdm(video_id_list, desc="📽️ Scraping YouTube videos"):
    try:
        df_tmp = getcomments(video_id)  # includes comments + replies + metadata
        df = pd.concat([df, df_tmp], ignore_index=True)
    except Exception as e:
        print(f"❌ Failed to process video {video_id}: {e}")

#####Save File to CSV, Can customize filename and directory for saving###########
df.to_csv("Youtube Scraping.csv", index=False)
df