import os, re, json, random, time, textwrap, glob, subprocess, urllib.parse, requests
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def run(c):
    if c[0] == "ffmpeg":
        c = ["ffmpeg", "-loglevel", "error", "-nostats"] + c[1:]
    subprocess.run(c, check=True)

client = genai.Client(api_key=os.environ["GEMINI_KEY"])

def ask(p):
    for attempt in range(6):
        for m in ["gemini-3.8-flash", "gemini-flash-latest"]:
            try:
                r = client.models.generate_content(model=m, contents=p)
                return json.loads(re.search(r"\{.*\}", r.text, re.S).group())
            except Exception as e:
                print("Gemini failed:", m, str(e)[:120])
        time.sleep(20 * (attempt + 1))
    raise SystemExit("Gemini unavailable")

STATE = "story.json"
if os.path.exists(STATE):
    st = json.load(open(STATE))
else:
    b = ask("""Invent an original anime-style serialized story premise for YouTube Shorts (mystery, adventure, sci-fi or romance-comedy) that makes people want to follow for the next part. Return ONLY JSON:
{"series_title":"max 40 chars","bible":"premise, main characters with names and personalities, the central mystery, about 80 words","look":"fixed visual description of the 2 main characters for image prompts: hair color, eye color, outfit, about 40 words"}""")
    st = {"episode": 0, "recap": "", "series_title": b["series_title"], "bible": b["bible"],
          "look": b["look"],
          "style": "anime style, cinematic lighting, vibrant colors, highly detailed, vertical poster composition"}

ep = st["episode"] + 1
d = ask(f"""You write a serialized anime-style short story told as a vertical video with on-screen text and no voice.
Series bible: {st['bible']}
Story so far: {st['recap'] or 'Nothing yet, this is the first episode.'}
Write episode {ep}. Exactly 8 scenes. Each scene has "text" (max 14 words, gripping, plain English; scene 1 is a hook) and "image" (what the picture shows: action, setting, mood, camera angle; max 30 words; do NOT describe the characters' faces or outfits, that is added automatically).
Scene 8 must end on a cliffhanger. Never end the whole story. Keep it fictional with nothing graphic or hateful.
Return ONLY JSON: {{"title":"episode title max 40 chars","recap_update":"3 sentences summarizing the whole story so far including this episode","scenes":[{{"text":"","image":""}}]}}""")

def make_image(desc, i):
    p = f"{st['style']}, {st['look']}, {desc}"[:700]
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(p) +
           f"?width=1080&height=1920&nologo=true&seed={random.randint(1, 99999)}")
    for a in range(5):
        try:
            resp = requests.get(url, timeout=120)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                open(f"img{i}.jpg", "wb").write(resp.content)
                return
            print("image status", resp.status_code)
        except Exception as e:
            print("image error", str(e)[:100])
        time.sleep(10 * (a + 1))
    raise SystemExit("image generation failed")

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
segs = []
for i, s in enumerate(d["scenes"][:8]):
    make_image(s["image"], i)
    open(f"t{i}.txt", "w").write(textwrap.fill(s["text"], 22))
    vf = ("scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840,"
          "zoompan=z='min(zoom+0.0012,1.25)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=150:s=1080x1920:fps=30,"
          f"drawtext=fontfile={FONT}:textfile=t{i}.txt:fontcolor=white:fontsize=60:"
          "borderw=6:bordercolor=black:line_spacing=10:x=(w-text_w)/2:y=h-text_h-260")
    run(["ffmpeg", "-y", "-i", f"img{i}.jpg", "-vf", vf, "-t", "5", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", f"seg{i}.mp4"])
    segs.append(f"seg{i}.mp4")

with open("list.txt", "w") as L:
    for f in segs:
        L.write(f"file '{f}'\n")
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"])

total = 5 * len(segs)
tracks = glob.glob("**/*.mp3", recursive=True)
if tracks:
    run(["ffmpeg", "-y", "-i", "joined.mp4", "-stream_loop", "-1", "-i", random.choice(tracks),
         "-map", "0:v", "-map", "1:a", "-t", str(total),
         "-af", f"volume=0.6,afade=t=out:st={total - 2}:d=2",
         "-c:v", "copy", "-c:a", "aac", "final.mp4"])
else:
    os.rename("joined.mp4", "final.mp4")

creds = Credentials(None, refresh_token=os.environ["YT_REFRESH"],
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"])
yt = build("youtube", "v3", credentials=creds)
title = f"{st['series_title'][:45]} - Part {ep}: {d['title'][:30]}"
yt.videos().insert(part="snippet,status",
  body={"snippet": {"title": title[:85] + " #Shorts",
        "description": f"Part {ep}. Follow for Part {ep + 1}!\nOriginal fictional story, art made with AI.\n\n#anime #story #shorts",
        "tags": ["anime", "story", "shorts", st["series_title"]], "categoryId": "1"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
  media_body=MediaFileUpload("final.mp4", resumable=True)).execute()

st["episode"] = ep
st["recap"] = d["recap_update"]
json.dump(st, open(STATE, "w"), indent=1)
