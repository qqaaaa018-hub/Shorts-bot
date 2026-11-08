import os, re, json, random, time, textwrap, subprocess, requests
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def run(c):
    if c[0] == "ffmpeg":
        c = ["ffmpeg", "-loglevel", "error", "-nostats"] + c[1:]
    subprocess.run(c, check=True)

THEMES = ["a cat who thinks he runs the house", "a robot learning to cook",
  "the worst first date ever", "a dog's secret double life",
  "a squirrel heist", "office coffee machine drama", "a ghost who is bad at haunting",
  "a pigeon with big plans", "a grandma who beats everyone at video games",
  "a goldfish with a three second memory", "an alien tourist visiting earth",
  "a sleepy panda's terrible Monday"]
theme = random.choice(THEMES)

client = genai.Client(api_key=os.environ["GEMINI_KEY"])
prompt = f"""Write an original, funny fictional mini-story for a silent YouTube Short
(on-screen text only). Theme: {theme}. It must be fiction, never claim it really happened.
Make exactly 6 scenes. Each scene has "text" (max 12 words, punchy, scene 1 is a hook,
scene 6 is a twist or punchline) and "search" (one or two simple words for a stock video,
concrete and visual like cat, kitchen, rain, city, ocean).
Return ONLY JSON: {{"title":"max 70 chars","description":"","tags":[],"scenes":[{{"text":"","search":""}}]}}"""

r = None
for attempt in range(6):
    for m in ["gemini-3.8-flash", "gemini-flash-latest"]:
        try:
            r = client.models.generate_content(model=m, contents=prompt)
            break
        except Exception as e:
            print("Gemini failed:", m, str(e)[:120])
    if r:
        break
    time.sleep(20 * (attempt + 1))
if not r:
    raise SystemExit("Gemini unavailable")
d = json.loads(re.search(r"\{.*\}", r.text, re.S).group())

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
segs = []
for i, s in enumerate(d["scenes"][:6]):
    hits = []
    for q in [s["search"], "nature"]:
        j = requests.get("https://pixabay.com/api/videos/",
            params={"key": os.environ["PIXABAY_KEY"], "q": q, "per_page": 5}).json()
        hits = j.get("hits", [])
        if hits:
            break
    v = random.choice(hits)["videos"]
    url = (v.get("medium") or v.get("small") or v["tiny"])["url"]
    open(f"raw{i}.mp4", "wb").write(requests.get(url).content)
    open(f"t{i}.txt", "w").write(textwrap.fill(s["text"], 18))
    vf = ("scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,"
          f"drawtext=fontfile={FONT}:textfile=t{i}.txt:fontcolor=white:fontsize=76:"
          "borderw=6:bordercolor=black:line_spacing=12:x=(w-text_w)/2:y=(h-text_h)/2")
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", f"raw{i}.mp4", "-t", "5",
         "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", f"seg{i}.mp4"])
    segs.append(f"seg{i}.mp4")

with open("list.txt", "w") as L:
    for f in segs:
        L.write(f"file '{f}'\n")
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
     "-c", "copy", "final.mp4"])

creds = Credentials(None, refresh_token=os.environ["YT_REFRESH"],
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"])
yt = build("youtube", "v3", credentials=creds)
yt.videos().insert(part="snippet,status",
  body={"snippet": {"title": d["title"][:90] + " #Shorts",
        "description": d["description"] + "\n\n#Shorts #funny #story",
        "tags": d["tags"], "categoryId": "23"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
  media_body=MediaFileUpload("final.mp4", resumable=True)).execute()
