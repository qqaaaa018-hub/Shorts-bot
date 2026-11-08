"""Shorts-bot: cartoon curiosity serial with narration and background music.
Builds one episode per run and uploads it as a YouTube Short."""
import asyncio
import json
import os
import pathlib
import random
import subprocess
import textwrap
import time
import urllib.parse

import edge_tts
import requests

ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work"
MUSIC_DIR = ROOT / "music"
STORY_FILE = ROOT / "story.json"

W, H, FPS = 720, 1280, 30
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CAPTION_FONT_SIZE = 40
CAPTION_WRAP = 18  # characters per line

# Settings (override in the workflow's env block)
VOICE = os.environ.get("TTS_VOICE", "en-US-GuyNeural")
TTS_PITCH = os.environ.get("TTS_PITCH", "+0Hz")
TTS_RATE = os.environ.get("TTS_RATE", "+5%")
GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
    os.environ.get("GEMINI_FALLBACK", "gemini-2.5-flash"),
]
PRIVACY = os.environ.get("YT_PRIVACY", "public")  # "private" if uploads are restricted
DRY_RUN = os.environ.get("DRY_RUN") == "1"        # build the video but don't upload

STYLE = ("3D animated cartoon, Pixar style, cute anthropomorphic animal characters with big "
         "expressive eyes, bright colorful background, vertical 9:16 composition, cartoon comedy. "
         "Not anime, not manga, not realistic photo. No text, no speech bubbles, no humans")


# ---------- helpers ----------

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("ffmpeg failed: " + p.stderr[-800:])


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout
    return "audio" in out


# ---------- story ----------

def load_story():
    if STORY_FILE.exists():
        return json.loads(STORY_FILE.read_text())
    return {"bible": None, "episodes": []}


def build_prompt(story, n):
    recaps = [e["recap"] for e in story["episodes"][-3:]]
    bible = story.get("bible") or "none yet: invent it"
    return f"""You write episode {n} of a funny cartoon series for YouTube Shorts (about 35 seconds).
Stories feature talking cartoon animals and vehicles: rivalries, races, silly challenges, and a curious question that keeps the viewer watching. Characters are cartoon animals or cartoon vehicles only. No humans, no anime-style characters.
Series bible (keep characters and look consistent; if none yet, create one): {bible}
Recent episodes: {json.dumps(recaps)}
Rules:
- Scene 1 is a hook in the first 2 seconds: a question or a funny challenge that makes the viewer curious.
- Each scene reveals one small new piece and opens a new question.
- The final scene ends on a cliffhanger.
- Original characters only. No real people, brands, existing cartoons, or song lyrics.
- Make 6 to 8 scenes. Narration: 8 to 14 words per scene, simple spoken English.
- caption: under 6 words, shown on screen. image: a concrete picture of this moment, with no text in it.
Return ONLY a JSON object with these keys:
"title" (under 60 characters, curiosity-driven, no spoilers),
"description" (two sentences, ending with #Shorts),
"tags" (list of strings),
"bible" (string describing characters and visual look; repeat the given one if it exists),
"recap" (one sentence summarizing this episode),
"scenes" (list of objects with "narration", "caption", "image")."""


def gemini_json(prompt):
    key = os.environ["GEMINI_KEY"]
    for model in GEMINI_MODELS:
        for attempt in range(4):
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"responseMimeType": "application/json",
                                           "temperature": 1.0}},
                timeout=180)
            if r.status_code == 200:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            if r.status_code == 404:
                print(f"model {model} not found, trying fallback")
                break
            if r.status_code in (429, 500, 503):
                print(f"{model} busy ({r.status_code}), retrying")
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
    raise RuntimeError("Gemini failed on all models")


# ---------- pictures and voice ----------

def fetch_image(prompt, path, seed):
    prompt = prompt[:600]  # keep the URL a reasonable length
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           f"?width={W}&height={H}&nologo=true&seed={seed}")
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                path.write_bytes(r.content)
                return
        except requests.RequestException:
            pass
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("image generation failed after 5 tries")


async def make_tts(text, path):
    await edge_tts.Communicate(text, VOICE, rate=TTS_RATE, pitch=TTS_PITCH).save(str(path))


def pad_audio(src, dst, dur):
    run(["ffmpeg", "-y", "-i", str(src),
         "-af", f"apad,atrim=0:{dur:.2f}", "-ar", "44100", "-ac", "2",
         "-c:a", "aac", "-b:a", "128k", str(dst)])


# ---------- video ----------

def build_scene(i, img, dur, cap_file):
    frames = round(dur * FPS)
    clip = WORK / f"clip{i}.mp4"
    vf = (f"scale=1440:2560,"
          f"zoompan=z='min(zoom+0.0012,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"d={frames}:s={W}x{H}:fps={FPS},"
          f"drawtext=fontfile={FONT}:textfile={cap_file}:fontsize={CAPTION_FONT_SIZE}:"
          f"fontcolor=white:borderw=3:bordercolor=black:line_spacing=8:"
          f"x=(w-text_w)/2:y=h*0.72,format=yuv420p")
    run(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-t", f"{dur:.2f}",
         "-vf", vf, "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
         "-an", str(clip)])
    return clip


def concat(files, out):
    lst = WORK / (out.stem + ".txt")
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    return out


def mix(video, narr, out):
    total = duration(video)
    tracks = sorted(MUSIC_DIR.glob("*.mp3")) if MUSIC_DIR.exists() else []
    if tracks:
        music = random.choice(tracks)
        fc = ("[2:a]volume=0.10[m];[1:a]volume=1.0[n];"
              "[n][m]amix=inputs=2:duration=first:dropout_transition=0[a]")
        cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(narr),
               "-stream_loop", "-1", "-i", str(music),
               "-filter_complex", fc, "-map", "0:v", "-map", "[a]"]
    else:
        print("no music tracks in music/, using narration only")
        cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(narr),
               "-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-t", f"{total:.2f}", "-movflags", "+faststart", str(out)]
    run(cmd)
    return out


# ---------- upload ----------

def yt_access_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH"],
        "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("YouTube token refresh failed (" + r.text[:200] +
                           "). Rerun get_token.py and update YT_REFRESH.")
    return r.json()["access_token"]


def upload(video, meta):
    token = yt_access_token()
    body = {
        "snippet": {
            "title": meta["title"][:95],
            "description": meta["description"],
            "tags": meta.get("tags", [])[:15],
            "categoryId": "24",
        },
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(os.path.getsize(video))},
        data=json.dumps(body), timeout=60)
    init.raise_for_status()
    with open(video, "rb") as f:
        up = requests.put(init.headers["Location"], data=f,
                          headers={"Content-Type": "video/mp4"}, timeout=900)
    up.raise_for_status()
    return up.json()["id"]


# ---------- main ----------

def main():
    WORK.mkdir(exist_ok=True)
    story = load_story()
    n = len(story["episodes"]) + 1

    ep = gemini_json(build_prompt(story, n))
    bible = ep.get("bible") or story.get("bible") or ""
    seed_base = random.randint(1, 10**6)

    clips, audios = [], []
    for i, sc in enumerate(ep["scenes"]):
        img = WORK / f"img{i}.jpg"
        raw = WORK / f"tts{i}.mp3"
        cap = WORK / f"cap{i}.txt"
        aud = WORK / f"aud{i}.m4a"

        fetch_image(f"{STYLE}. {bible}. {sc['image']}", img, seed_base + i)
        asyncio.run(make_tts(sc["narration"], raw))
        dur = max(3.0, duration(raw) + 0.4)
        cap.write_text(textwrap.fill(sc["caption"], width=CAPTION_WRAP))
        pad_audio(raw, aud, dur)
        clips.append(build_scene(i, img, dur, cap))
        audios.append(aud)
        print(f"scene {i + 1} ready ({dur:.1f}s)")

    video_only = concat(clips, WORK / "video_only.mp4")
    narr = concat(audios, WORK / "narration.m4a")
    final = mix(video_only, narr, WORK / "final.mp4")

    if not has_audio(final):
        raise RuntimeError("final video has no audio track; not uploading")
    print(f"built episode {n} ({duration(final):.1f}s, with audio)")

    if DRY_RUN:
        print("DRY_RUN=1: skipping upload and story save")
        return

    video_id = upload(final, ep)
    print(f"uploaded https://youtu.be/{video_id}")

    story["bible"] = bible
    story["episodes"].append({"n": n, "title": ep["title"], "recap": ep["recap"]})
    STORY_FILE.write_text(json.dumps(story, indent=2))


if __name__ == "__main__":
    main()
