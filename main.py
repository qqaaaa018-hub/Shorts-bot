import os, re, json, subprocess, requests, feedparser
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def run(c): subprocess.run(c, check=True)

# 1. TOPIC
feed = feedparser.parse("https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en")
heads = [e.title for e in feed.entries[:6]]

# 2. SCRIPT
client = genai.Client(api_key=os.environ["GEMINI_KEY"])
prompt = f"""Headlines: {heads}
Pick the single most interesting one. Write a YouTube Shorts script, 90-110 words,
strong hook in the first sentence, plain spoken English, no emojis, no stage directions,
only claims supported by the headline. Return ONLY JSON:
{{"title":"","script":"","keywords":["3-4 visual stock-footage search terms"],"description":"","tags":[]}}"""
r = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
d = json.loads(re.search(r"\{.*\}", r.text, re.S).group())

# 3. VOICE + SUBTITLES
open("script.txt", "w").write(d["script"])
run(["edge-tts", "-f", "script.txt", "--voice", "en-US-AndrewNeural",
     "--write-media", "voice.mp3", "--write-subtitles", "subs.vtt"])
dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
      "format=duration", "-of", "csv=p=0", "voice.mp3"]))

# 4. FOOTAGE (Pixabay)
segs = []
for i, k in enumerate(d["keywords"][:4]):
    j = requests.get("https://pixabay.com/api/videos/",
        params={"key": os.environ["PIXABAY_KEY"], "q": k, "per_page": 3}).json()
    if not j.get("hits"):
        continue
    v = j["hits"][0]["videos"]
    url = (v.get("medium") or v.get("small") or v["tiny"])["url"]
    open(f"raw{i}.mp4", "wb").write(requests.get(url).content)
    segs.append(f"raw{i}.mp4")
if not segs:
    raise SystemExit("no footage found")
seg = dur / len(segs) + 0.3
with open("list.txt", "w") as L:
    for i, f in enumerate(segs):
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", f, "-t", str(seg),
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
             "-an", "-c:v", "libx264", "-preset", "veryfast", f"seg{i}.mp4"])
        L.write(f"file 'seg{i}.mp4'\n")

# 5. ASSEMBLE
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-i", "voice.mp3",
     "-vf", "subtitles=subs.vtt:force_style='FontSize=18,Bold=1,Outline=2,Alignment=2,MarginV=120'",
     "-map", "0:v", "-map", "1:a", "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast",
     "-c:a", "aac", "final.mp4"])

# 6. UPLOAD
creds = Credentials(None, refresh_token=os.environ["YT_REFRESH"],
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"])
yt = build("youtube", "v3", credentials=creds)
yt.videos().insert(part="snippet,status",
  body={"snippet": {"title": d["title"][:90] + " #Shorts",
        "description": d["description"] + "\n\n#Shorts",
        "tags": d["tags"], "categoryId": "28"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
  media_body=MediaFileUpload("final.mp4", resumable=True)).execute()
