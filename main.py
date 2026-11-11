#!/usr/bin/env python3
"""
Vintage-cartoon "talking cat" Shorts generator.

Pipeline:  Gemini (script + scene prompts) -> Gemini image model (art)
           -> edge-tts (voice + word timings) -> ffmpeg (Ken Burns + word captions)

Then uploads to YouTube Shorts and records the episode in story.json.

Env (same secret names as the old bot):
  GEMINI_KEY, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH
Optional: VIDEO_LANG (en / hi / ml, default en), TOPIC, YT_PRIVACY (public/private),
          DRY_RUN=1 (build only, no upload), TTS_VOICE, TTS_RATE, TTS_PITCH
Run:     python main.py            (Gemini picks a fresh topic)
         python main.py "why sleeping 8 hours is a myth"
Needs:   pip install requests edge-tts   (+ ffmpeg and DejaVu fonts)
"""
import os, sys, json, time, random, base64, asyncio, subprocess, re, shutil, pathlib
import requests

# ============================ CONFIG ============================
ROOT = pathlib.Path(__file__).resolve().parent
API_KEY = os.environ.get("GEMINI_KEY") or os.environ.get("GEMINI_API_KEY", "")
STORY_FILE = ROOT / "story.json"
MUSIC_DIR = ROOT / "music"
PRIVACY = os.environ.get("YT_PRIVACY", "public")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
TEXT_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite",
               "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.5-pro"]
IMAGE_MODELS = ["gemini-2.5-flash-image", "gemini-3.1-flash-image"]
LANGS = {
    "en": {"name": "English",   "voice": "en-US-AndrewMultilingualNeural", "words": "140-165",
           "font": "DejaVu Serif", "size": 84, "engine": "edge"},
    "hi": {"name": "Hindi",     "voice": "hi-IN-MadhurNeural", "words": "130-155",
           "font": "Noto Serif Devanagari", "size": 76, "engine": "gemini"},
    "ml": {"name": "Malayalam", "voice": "ml-IN-MidhunNeural", "words": "95-120",
           "font": "Noto Serif Malayalam", "size": 70, "engine": "gemini"},
}
LANG = os.environ.get("VIDEO_LANG", "en").strip().lower()
if LANG not in LANGS:
    sys.exit("VIDEO_LANG must be one of: " + ", ".join(LANGS))
LC = LANGS[LANG]
VOICE = os.environ.get("TTS_VOICE") or LC["voice"]
TTS_ENGINE = os.environ.get("TTS_ENGINE", LC["engine"])      # "gemini" or "edge"
GEMINI_VOICE = os.environ.get("GEMINI_VOICE", "Charon")      # try Puck, Fenrir, Orus, Kore, Aoede
GEMINI_TTS_MODELS = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview"]
VOICE_RATE = os.environ.get("TTS_RATE", "+0%")
VOICE_PITCH = os.environ.get("TTS_PITCH", "+0Hz")
W, H, FPS = 720, 1280, 30
OUT = str(ROOT / "output")
PAD = 0.25                          # silence after each scene (seconds)
CAPTION_FONT = LC["font"]
CAPTION_SIZE = LC["size"]
CAPTION_MARGIN_V = 330              # distance from bottom

CAT = ("a cute grey cartoon cat narrator wearing a grey fedora hat, dark green suit, white shirt "
       "and dark red tie, large round friendly eyes with black pupils and white highlights, "
       "both eyes symmetrical and looking the same direction")

STYLE = ("clean modern 2D cartoon illustration with a retro 1940s feel, bold smooth outlines, "
         "sharp crisp high-detail artwork, rich colors, no film grain, no noise, no blur, "
         "no vignette, vertical 9:16 composition, NO text, NO letters, NO captions, NO watermark")

LOOKS = {
    "cat":     "medium shot from the waist up, warm amber lighting, cozy retro room, full color. Character: " + CAT,
    "story":   "muted warm storybook colors, clean sharp lines, soft window or candle light",
    "chalk":   "black chalkboard background, white chalk line-art diagram with a hand pointing a stick, "
               "simple icons (clock, magnifying glass, numbers), no words",
    "collage": "cream paper background, sticker-style paper cutouts and icons arranged as a collage, "
               "soft pastel tones, one central character",
}


def log(*a):
    print(*a, flush=True)


# ============================ GEMINI TEXT ============================
def gemini_json(prompt, rounds=6):
    last = None
    for rnd in range(rounds):
        for m in TEXT_MODELS:
            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % m,
                    params={"key": API_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json",
                                               "temperature": 0.9}},
                    timeout=90)
            except Exception as e:
                last = "%s: %s" % (m, e)
                log("  %s error: %s" % (m, str(e)[:100]))
                continue
            if r.status_code == 200:
                try:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
                    data = json.loads(txt)
                    log("  script written by", m)
                    return data
                except Exception as e:
                    last = "%s bad output: %s" % (m, e)
                    log("  %s returned unusable output, next" % m)
                    continue
            last = "%s %s: %s" % (m, r.status_code, r.text[:120].replace("\n", " "))
            log("  %s -> %s" % (m, r.status_code))
        wait = min(30 * (rnd + 1), 150) + random.uniform(0, 5)
        log("  all models busy, waiting %ds (round %d/%d)" % (wait, rnd + 1, rounds))
        time.sleep(wait)
    raise RuntimeError("Gemini text failed: %s" % last)


def write_script(topic, story):
    used = [e.get("title", "") for e in story.get("episodes", [])][-40:]
    if not topic:
        topic = ("pick a fresh surprising myth or curiosity (history, psychology, science, habits). "
                 "Do NOT repeat these earlier videos: %s" % json.dumps(used))
    prompt = """You write viral 60-second YouTube Shorts in the style of a curious grey cat detective
who busts myths. Topic: "@@TOPIC@@"

LANGUAGE: write title, description, tags and ALL narration in @@LANG@@ (natural, conversational,
the way a friendly person actually talks; short sentences; use the native script, not
transliteration). Keep the "image" descriptions in English.

Return ONLY JSON:
{
 "title": "catchy title under 70 chars",
 "description": "1-2 sentence description ending with #Shorts",
 "tags": ["history", "psychology", ...],
 "recap": "one sentence summary of this episode",
 "scenes": [
   {"type": "cat|story|chalk|collage",
    "narration": "1-2 short spoken sentences",
    "image": "visual description of the scene, no text in image"}
 ]
}

Rules:
- 10 to 12 scenes, total narration @@WORDS@@ words (about 60 seconds spoken).
- Scene 1 type "cat": a shocking hook question that makes people stay.
- Last scene type "cat": a punchy conclusion plus a question to the viewer.
- "story" scenes tell a short human example (a man, a bed, a clock, a village...).
- Use 1-2 "chalk" scenes for the explanation / numbers, and 1 "collage" scene for the evidence.
- Simple spoken @@LANG@@, no emojis, no stage directions in narration.
- Image descriptions describe ONLY the picture (characters, setting, action, mood)."""
    prompt = (prompt.replace("@@TOPIC@@", topic).replace("@@LANG@@", LC["name"])
                    .replace("@@WORDS@@", LC["words"]))
    data = gemini_json(prompt)
    scenes = data["scenes"]
    if "#Shorts" not in data.get("description", ""):
        data["description"] = data.get("description", "") + " #Shorts"
    for s in scenes:
        if s.get("type") not in LOOKS:
            s["type"] = "story"
    return data


# ============================ IMAGES ============================
GEMINI_IMG_OK = True     # switched off for the rest of the run if the key has no image quota
SHORT_STYLE = ("clean modern 2D cartoon illustration, retro 1940s feel, bold smooth outlines, "
               "sharp high detail, rich colors, no grain, no noise, vertical composition, "
               "no text, no letters")


def gemini_image(prompt, path):
    for m in IMAGE_MODELS:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % m,
                params={"key": API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"responseModalities": ["IMAGE"],
                                           "imageConfig": {"aspectRatio": "9:16"}}},
                timeout=120)
            if r.status_code == 400 and "imageConfig" in r.text:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % m,
                    params={"key": API_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseModalities": ["IMAGE"]}},
                    timeout=120)
            if r.status_code != 200:
                log("  gemini image %s -> %s" % (m, r.status_code))
                continue
            for part in r.json()["candidates"][0]["content"]["parts"]:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(inline["data"]))
                    return True
        except Exception as e:
            log("  gemini image error %s: %s" % (m, str(e)[:100]))
    return False


def pollinations_image(scene, idx, path):
    import urllib.parse
    prompt = "%s, %s, %s" % (scene["image"], LOOKS[scene["type"]], SHORT_STYLE)
    prompt = prompt[:700]
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           "?width=1080&height=1920&nologo=true&model=flux&enhance=true&seed=%d" % (1000 + idx))
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                with open(path, "wb") as f:
                    f.write(r.content)
                return True
            log("  pollinations -> %s" % r.status_code)
        except Exception as e:
            log("  pollinations error: %s" % str(e)[:100])
        time.sleep(6 * (attempt + 1))
    return False


def gen_image(scene, idx, path):
    global GEMINI_IMG_OK
    if GEMINI_IMG_OK:
        prompt = "%s. %s. Scene: %s" % (STYLE, LOOKS[scene["type"]], scene["image"])
        if gemini_image(prompt, path):
            return True
        GEMINI_IMG_OK = False
        log("  Gemini images unavailable (quota?), using Pollinations for the rest of this run")
    return pollinations_image(scene, idx, path)


# ============================ VOICE ============================
async def _tts(text, mp3_path):
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH, boundary="WordBoundary")
    words = []
    with open(mp3_path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                start = ch["offset"] / 1e7
                words.append((ch["text"], start, start + ch["duration"] / 1e7))
    return words


def make_voice(text, mp3_path):
    for attempt in range(3):
        try:
            words = asyncio.run(_tts(text, mp3_path))
            if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 500:
                raise RuntimeError("empty audio")
            if words:
                return words
            # voice gave audio but no word timings: spread the words evenly over the audio
            toks = text.split()
            dur = probe_duration(mp3_path)
            step = dur / max(len(toks), 1)
            return [(t, i * step, (i + 1) * step) for i, t in enumerate(toks)]
        except Exception as e:
            log("  tts error:", str(e)[:120])
        time.sleep(3)
    raise RuntimeError("TTS failed for: " + text[:50])


def spread_words(text, dur):
    """Approximate word timings (Gemini TTS gives none): weight by length, extra for pauses."""
    toks = text.split()
    weights = []
    for t in toks:
        w = len(t) + 1.0
        if t[-1:] in ",;:":
            w += 3
        elif t[-1:] in ".?!।":
            w += 6
        weights.append(w)
    total = sum(weights) or 1.0
    lead, usable = 0.08, max(dur - 0.12, 0.1)
    out, t = [], lead
    for tok, w in zip(toks, weights):
        d = usable * w / total
        out.append((tok, t, t + d))
        t += d
    return out


def gemini_tts(text, mp3_path):
    """One scene of narration -> mp3. Returns word timings or None on failure."""
    prompt = "Say this in a warm, natural, expressive storytelling voice, like a friendly person " \
             "talking to a friend, with clear native %s pronunciation: %s" % (LC["name"], text)
    for model in GEMINI_TTS_MODELS:
        for attempt in range(4):
            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
                    params={"key": API_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {
                              "responseModalities": ["AUDIO"],
                              "speechConfig": {"voiceConfig": {
                                  "prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}}}},
                    timeout=120)
                if r.status_code in (429, 500, 503):
                    log("  gemini tts %s -> %s" % (model, r.status_code))
                    time.sleep(10 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    log("  gemini tts %s -> %s %s" % (model, r.status_code, r.text[:120]))
                    break
                part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                m = re.search(r"rate=(\d+)", part.get("mimeType", ""))
                rate = m.group(1) if m else "24000"
                raw = mp3_path + ".pcm"
                with open(raw, "wb") as f:
                    f.write(base64.b64decode(part["data"]))
                run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", rate, "-ac", "1",
                     "-i", raw, "-ar", "44100", "-b:a", "160k", mp3_path])
                os.remove(raw)
                return spread_words(text, probe_duration(mp3_path))
            except Exception as e:
                log("  gemini tts error %s: %s" % (model, str(e)[:120]))
                time.sleep(5)
    return None


def make_all_voice(scenes, work):
    """Prefer Gemini voice for the whole video; if any scene fails, redo ALL scenes with
    edge-tts so one video never mixes two different voices."""
    done = False
    if TTS_ENGINE == "gemini":
        ok = True
        for i, s in enumerate(scenes):
            mp3 = os.path.join(work, "voice_%02d.mp3" % i)
            words = gemini_tts(s["narration"], mp3)
            if not words:
                ok = False
                break
            s["words"], s["mp3"] = words, mp3
            s["dur"] = probe_duration(mp3) + PAD
            log("   voice %d/%d (gemini)" % (i + 1, len(scenes)))
            time.sleep(6)          # stay under the per-minute limit
        done = ok
        if not ok:
            log("  Gemini voice unavailable, using edge-tts voice for the whole video")
    if not done:
        for i, s in enumerate(scenes):
            mp3 = os.path.join(work, "voice_%02d.mp3" % i)
            s["words"] = make_voice(s["narration"], mp3)
            s["mp3"] = mp3
            s["dur"] = probe_duration(mp3) + PAD


def probe_duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


# ============================ VIDEO ============================
def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-1500:])


def scene_clip(img, mp3, dur, idx, out_path):
    n = int(dur * FPS) + 1
    mode = idx % 4
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if mode == 0:      # slow push in
        z, x, y = "1+0.16*on/%d" % n, cx, cy
    elif mode == 1:    # slow pull out
        z, x, y = "1.16-0.16*on/%d" % n, cx, cy
    elif mode == 2:    # pan left to right
        z, x, y = "1.2", "(iw-iw/zoom)*on/%d" % n, cy
    else:              # pan right to left
        z, x, y = "1.2", "(iw-iw/zoom)*(1-on/%d)" % n, cy
    # upscale first so zoompan moves smoothly (no pixel jitter)
    vf = ("scale=2160:3840:force_original_aspect_ratio=increase:flags=lanczos,crop=2160:3840,"
          "zoompan=z='%s':d=%d:x='%s':y='%s':s=%dx%d:fps=%d,format=yuv420p") % (z, n, x, y, W, H, FPS)
    run(["ffmpeg", "-y", "-v", "error", "-i", img, "-i", mp3,
         "-vf", vf, "-af", "apad=pad_dur=%f" % (PAD + 0.5),
         "-t", "%.3f" % dur,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", out_path])


def ass_time(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def write_ass(all_words, path):
    """all_words: list of (text, start, end) in global time. One word on screen at a time."""
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: W,%s,%d,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,40,40,%d,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
            % (W, H, CAPTION_FONT, CAPTION_SIZE, CAPTION_MARGIN_V))
    lines = []
    for i, (txt, st, en) in enumerate(all_words):
        end = all_words[i + 1][1] if i + 1 < len(all_words) and all_words[i + 1][1] - en < 0.4 else en + 0.15
        clean = txt.replace("{", "").replace("}", "").replace("\\", "")
        lines.append("Dialogue: 0,%s,%s,W,,0,0,0,,%s" % (ass_time(st), ass_time(end), clean))
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")


def build_video(scenes, workdir, final_path):
    """scenes: list of dict(img, mp3, words, dur)."""
    clips, all_words, t0 = [], [], 0.0
    for i, s in enumerate(scenes):
        clip = os.path.join(workdir, "clip_%02d.mp4" % i)
        scene_clip(s["img"], s["mp3"], s["dur"], i, clip)
        clips.append(clip)
        for (w, a, b) in s["words"]:
            all_words.append((w, t0 + a, t0 + b))
        t0 += s["dur"]
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        for c in clips:
            f.write("file '%s'\n" % os.path.basename(c))
    joined = os.path.join(workdir, "joined.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
    write_ass(all_words, os.path.join(workdir, "captions.ass"))
    run(["ffmpeg", "-y", "-v", "error", "-i", joined, "-vf",
         "ass=%s" % os.path.join(workdir, "captions.ass"),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy",
         "-movflags", "+faststart", final_path])


def add_music(video, out):
    tracks = sorted(MUSIC_DIR.glob("*.mp3")) if MUSIC_DIR.exists() else []
    if not tracks:
        log("   no music/*.mp3 found, narration only")
        shutil.copy(video, out)
        return
    music = random.choice(tracks)
    run(["ffmpeg", "-y", "-v", "error", "-i", video, "-stream_loop", "-1", "-i", str(music),
         "-filter_complex",
         "[1:a]volume=0.10[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", out])


# ============================ YOUTUBE ============================
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
        "snippet": {"title": meta["title"][:95], "description": meta["description"],
                    "tags": meta.get("tags", [])[:15], "categoryId": "24",
                    "defaultLanguage": LANG, "defaultAudioLanguage": LANG},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={"Authorization": "Bearer " + token,
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


def load_story():
    try:
        data = json.loads(STORY_FILE.read_text())
        if isinstance(data, dict) and isinstance(data.get("episodes"), list):
            return data
    except Exception:
        pass   # missing, empty or broken file: start fresh
    return {"bible": None, "episodes": []}


# ============================ MAIN ============================
def main():
    if not API_KEY:
        sys.exit("Set GEMINI_KEY")
    topic = " ".join(sys.argv[1:]).strip() or os.environ.get("TOPIC", "").strip()
    story = load_story()
    n = len(story["episodes"]) + 1
    work = os.path.join(OUT, "work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    log("1/4 writing script for:", topic)
    data = write_script(topic, story)
    scenes = data["scenes"]
    log("   %d scenes, title: %s" % (len(scenes), data.get("title")))

    log("2/4 generating images")
    prev = None
    for i, s in enumerate(scenes):
        path = os.path.join(work, "img_%02d.png" % i)
        ok = gen_image(s, i, path)
        if not ok:
            if prev:
                log("   scene %d: image failed, reusing previous" % i)
                shutil.copy(prev, path)
            else:
                sys.exit("First image failed; check API key / image model access.")
        s["img"] = path
        prev = path
        log("   image %d/%d" % (i + 1, len(scenes)))
        time.sleep(1.5)

    log("3/4 generating voice")
    make_all_voice(scenes, work)

    log("4/4 rendering video")
    captioned = os.path.join(work, "captioned.mp4")
    build_video(scenes, work, captioned)
    final = os.path.join(OUT, "video.mp4")
    add_music(captioned, final)

    meta = {"title": data.get("title"), "description": data.get("description"),
            "tags": data.get("tags", [])}
    with open(os.path.join(OUT, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    total = sum(s["dur"] for s in scenes)
    log("built episode %d: %s (%.1fs)" % (n, final, total))

    if DRY_RUN:
        log("DRY_RUN=1: skipping upload and story save")
        return
    video_id = upload(final, data)
    log("uploaded https://youtu.be/%s" % video_id)
    story["bible"] = CAT
    story["episodes"].append({"n": n, "lang": LANG, "title": data.get("title", ""),
                              "recap": data.get("recap", "")})
    STORY_FILE.write_text(json.dumps(story, indent=2))


if __name__ == "__main__":
    main()
