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
import os, sys, json, time, random, base64, asyncio, subprocess, re, shutil, pathlib, textwrap
import requests

# ============================ CONFIG ============================
ROOT = pathlib.Path(__file__).resolve().parent
API_KEY = os.environ.get("GEMINI_KEY") or os.environ.get("GEMINI_API_KEY", "")
STORY_FILE = ROOT / "story.json"
MUSIC_DIR = ROOT / "music"
VIDEO_MODE = os.environ.get("VIDEO_MODE", "quiz").strip().lower()   # "quiz", "puzzle" or "myth"
PRIVACY = os.environ.get("YT_PRIVACY", "public")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
TEXT_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite",
               "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-2.5-pro"]
IMAGE_MODELS = ["gemini-2.5-flash-image", "gemini-3.1-flash-image"]
LANGS = {
    "en": {"name": "English",   "voice": "en-US-AndrewMultilingualNeural", "words": "95-115",
           "font": "DejaVu Serif", "size": 84, "engine": "edge"},
    "hi": {"name": "Hindi",     "voice": "hi-IN-MadhurNeural", "words": "90-110",
           "font": "Noto Serif Devanagari", "size": 76, "engine": "gemini"},
    "ml": {"name": "Malayalam", "voice": "ml-IN-MidhunNeural", "words": "65-85",
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
MAX_SCENES = int(os.environ.get("MAX_SCENES", "8"))   # hard cap = max images per video
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
    "board":   "dark green chalkboard in a wooden frame, completely EMPTY and blank with no writing "
               "or drawings on it, cozy classroom lighting, nothing in front of the board",
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


def _norm(x):
    return re.sub(r"[^0-9a-z\u0900-\u097f\u0d00-\u0d7f]+", "", str(x).lower())


def same_answer(a, b):
    na, nb = re.findall(r"-?\d+(?:\.\d+)?", str(a)), re.findall(r"-?\d+(?:\.\d+)?", str(b))
    if na and nb:
        return na == nb
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x == y or x in y or y in x)


def verify_puzzle(puzzle, claimed):
    """Ask Gemini to solve the puzzle independently, so we never publish a wrong answer."""
    try:
        res = gemini_json(
            'Solve this puzzle carefully, thinking step by step. Return ONLY JSON: '
            '{"answer": "final answer only, as short as possible", '
            '"has_single_correct_answer": true or false}\n\nPuzzle: ' + puzzle, rounds=2)
    except Exception as e:
        log("  verify failed:", str(e)[:100])
        return False
    ok = bool(res.get("has_single_correct_answer", True)) and same_answer(res.get("answer", ""), claimed)
    log("  puzzle check: claimed=%r solver=%r -> %s" % (claimed, res.get("answer"), "OK" if ok else "MISMATCH"))
    return ok


def write_puzzle_script(topic, story):
    used = [e.get("title", "") for e in story.get("episodes", [])][-40:]
    kinds = ["a number sequence", "a classic logic trap question", "a short word riddle",
             "a quick maths trick question", "a lateral-thinking puzzle", "a pattern puzzle with numbers",
             "an age or family-relations puzzle", "a time or clock puzzle"]
    if not topic:
        topic = "%s that most people get wrong the first time" % random.choice(kinds)
    prompt = """You write viral 40-second YouTube Shorts: a puzzle that confuses and challenges people.
A friendly grey cat detective narrates. Puzzle idea: "@@TOPIC@@"
Do NOT repeat these earlier videos: @@USED@@

LANGUAGE: write title, description, tags, narration and the on-screen text in @@LANG@@ (natural,
conversational, short sentences, native script). Keep the "image" descriptions in English.

The puzzle must be fair, solvable in the viewer's head in about 5 seconds of thought, have exactly ONE
correct answer, and be easy to say aloud and read on screen. Prefer puzzles built on numbers or simple
logic so they work in any language. Never use real people, brands or copyrighted puzzles.

Return ONLY JSON:
{
 "title": "curiosity title under 70 chars, challenge style (no spoiler)",
 "description": "1-2 sentences ending with #Shorts #puzzle #riddle",
 "tags": ["puzzle", "riddle", "brain teaser", ...],
 "recap": "one sentence",
 "puzzle": "the full puzzle in one clear text",
 "answer": "the correct final answer, as short as possible",
 "scenes": [
  {"type": "cat|board", "narration": "spoken text", "image": "picture description, no text in it",
   "overlay": ["short on-screen line", "..."], "countdown": false}
 ]
}

Scenes, EXACTLY 7 in this order:
1. type "cat": hook, a challenge under 10 words (for example: can you solve this before the timer ends?). No greeting.
   Do NOT invent statistics or percentages.
2. type "board": narration reads the puzzle aloud. "overlay" = the puzzle as 2-4 short lines
   (max 20 characters per line).
3. type "board": repeat the key rule or the tempting wrong answer ("most people say X"). overlay optional.
4. type "cat": narration "Pause and think. Five seconds. Go!" (short). Set "countdown": true.
   "overlay" = the puzzle lines again.
5. type "board": narration reveals the answer. "overlay" = the answer (1-2 short lines).
6. type "board": short, clear explanation of WHY (2 sentences). overlay optional.
7. type "cat": ask "Did you get it? Comment your answer" and tell them to follow for a harder one
   tomorrow; the last words should flow back into the first line so the video loops.

Total narration @@WORDS@@ words. "board" images: describe a blank chalkboard scene only (no writing on it).
No emojis, no stage directions in narration.""".replace("@@TOPIC@@", topic) \
        .replace("@@USED@@", json.dumps(used)).replace("@@LANG@@", LC["name"]) \
        .replace("@@WORDS@@", LC["words"])
    for attempt in range(3):
        data = gemini_json(prompt)
        if len(data.get("scenes", [])) < 5 or not data.get("puzzle") or not data.get("answer"):
            log("  incomplete puzzle script, retrying")
            continue
        if verify_puzzle(data["puzzle"], data["answer"]):
            break
        log("  answer not confirmed, writing another puzzle (%d/3)" % (attempt + 1))
    else:
        raise RuntimeError("Could not produce a verified puzzle")
    data["scenes"] = data["scenes"][:MAX_SCENES]
    if "#Shorts" not in data.get("description", ""):
        data["description"] = data.get("description", "") + " #Shorts"
    for sc in data["scenes"]:
        if sc.get("type") not in LOOKS:
            sc["type"] = "board"
        ov = sc.get("overlay") or []
        if isinstance(ov, str):
            ov = [ov]
        sc["overlay"] = [str(x) for x in ov if str(x).strip()]
    return data


def write_script(topic, story):
    if VIDEO_MODE == "puzzle":
        return write_puzzle_script(topic, story)
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
- EXACTLY 7 or 8 scenes, total narration @@WORDS@@ words (about 40 seconds spoken). Short and punchy.
- Scene 1 type "cat": a shocking claim or question of under 10 words that makes people stay (no greeting, no intro).
- Last scene type "cat": a punchy conclusion whose final line leads naturally back into the opening line, so the video feels like a loop, plus one question to the viewer.
- "story" scenes tell a short human example (a man, a bed, a clock, a village...).
- Use 1 "chalk" scene for the explanation / numbers and at most 1 "collage" scene for the evidence.
- Simple spoken @@LANG@@, no emojis, no stage directions in narration.
- Image descriptions describe ONLY the picture (characters, setting, action, mood)."""
    prompt = (prompt.replace("@@TOPIC@@", topic).replace("@@LANG@@", LC["name"])
                    .replace("@@WORDS@@", LC["words"]))
    data = gemini_json(prompt)
    data["scenes"] = data["scenes"][:MAX_SCENES]
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


POLL_OK = True
CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
CF_OK = bool(CF_ACCOUNT and CF_TOKEN)


def to_vertical(path):
    """Turn any image into 1080x1920 (crop if already tall, blurred fill if square/wide)."""
    out = path + ".v.png"
    run(["ffmpeg", "-y", "-v", "error", "-i", path, "-filter_complex",
         "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:3[bg];"
         "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
         "[bg][fg]overlay=(W-w)/2:(H-h)/2", "-frames:v", "1", out])
    os.replace(out, path)


def cloudflare_image(prompt, path):
    url = ("https://api.cloudflare.com/client/v4/accounts/%s/ai/run/"
           "@cf/black-forest-labs/flux-1-schnell" % CF_ACCOUNT)
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"Authorization": "Bearer " + CF_TOKEN},
                              json={"prompt": prompt[:2000], "steps": 4}, timeout=120)
            if r.status_code == 200:
                b64 = (r.json().get("result") or {}).get("image")
                if b64:
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(b64))
                    return True
            log("  cloudflare image -> %s %s" % (r.status_code, r.text[:100].replace("\n", " ")))
            if r.status_code in (401, 403):
                return False
        except Exception as e:
            log("  cloudflare image error: %s" % str(e)[:100])
        time.sleep(5 * (attempt + 1))
    return False


def pollinations_image(scene, idx, path):
    global POLL_OK
    import urllib.parse
    prompt = ("%s, %s, %s" % (scene["image"], LOOKS[scene["type"]], SHORT_STYLE))[:700]
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           "?width=1080&height=1920&nologo=true&model=flux&seed=%d" % (1000 + idx))
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                with open(path, "wb") as f:
                    f.write(r.content)
                return True
            log("  pollinations -> %s" % r.status_code)
            if r.status_code in (401, 402, 403):     # now needs payment/key: stop trying
                POLL_OK = False
                return False
        except Exception as e:
            log("  pollinations error: %s" % str(e)[:100])
        time.sleep(6 * (attempt + 1))
    return False


def gen_image(scene, idx, path):
    global GEMINI_IMG_OK
    ok = False
    if GEMINI_IMG_OK:
        prompt = "%s. %s. Scene: %s" % (STYLE, LOOKS[scene["type"]], scene["image"])
        ok = gemini_image(prompt, path)
        if not ok:
            GEMINI_IMG_OK = False
            log("  Gemini images unavailable (quota?), switching source for this run")
    if not ok and CF_OK:
        prompt = "%s, %s, %s, tall vertical composition" % (scene["image"], LOOKS[scene["type"]], SHORT_STYLE)
        ok = cloudflare_image(prompt, path)
    if not ok and POLL_OK:
        ok = pollinations_image(scene, idx, path)
    if ok:
        try:
            to_vertical(path)
        except Exception as e:
            log("  could not process image: %s" % str(e)[:150])
            return False
    return ok


# ============================ VOICE ============================
FALLBACK_VOICE = {"en": "en-US-GuyNeural", "hi": "hi-IN-SwaraNeural", "ml": "ml-IN-SobhanaNeural"}
_TTS_IDX = 0     # index of the edge-tts settings that worked; reused so one video keeps one voice


async def _tts(text, mp3_path, voice, rate, pitch, boundary):
    import edge_tts
    kw = {}
    if rate:
        kw["rate"] = rate
    if pitch:
        kw["pitch"] = pitch
    if boundary:
        kw["boundary"] = boundary
    comm = edge_tts.Communicate(text, voice, **kw)
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
    """edge-tts with progressively simpler settings, because the service is picky."""
    global _TTS_IDX
    cfgs = [
        (VOICE, VOICE_RATE, VOICE_PITCH, "WordBoundary"),
        (VOICE, VOICE_RATE, VOICE_PITCH, None),
        (VOICE, None, None, None),
        (FALLBACK_VOICE[LANG], None, None, None),
    ]
    for idx in range(_TTS_IDX, len(cfgs)):
        voice, rate, pitch, boundary = cfgs[idx]
        for attempt in range(2):
            try:
                words = asyncio.run(_tts(text, mp3_path, voice, rate, pitch, boundary))
                if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 500:
                    raise RuntimeError("empty audio")
                if idx != _TTS_IDX:
                    log("  voice settings #%d work (%s), using them from now on" % (idx + 1, voice))
                    _TTS_IDX = idx
                return words or spread_words(text, probe_duration(mp3_path))
            except Exception as e:
                log("  tts error (settings #%d): %s" % (idx + 1, str(e)[:100]))
                time.sleep(2)
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
        for attempt in range(2):
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
                    if r.status_code == 429 and attempt == 1:
                        break
                    time.sleep(12)
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
        t_start = time.time()
        for i, s in enumerate(scenes):
            if time.time() - t_start > 480:      # never spend more than 8 minutes on this
                ok = False
                break
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
          "zoompan=z='%s':d=%d:x='%s':y='%s':s=%dx%d:fps=%d,"
          "fade=t=in:st=0:d=0.15,fade=t=out:st=%.2f:d=0.15,format=yuv420p") % (
              z, n, x, y, W, H, FPS, max(dur - 0.15, 0))
    run(["ffmpeg", "-y", "-v", "error", "-i", img, "-i", mp3,
         "-vf", vf, "-af", "apad",
         "-t", "%.3f" % dur,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", out_path])


def ass_time(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def write_ass(all_words, path, overlays=()):
    """all_words: list of (text, start, end) in global time. One word on screen at a time."""
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: W,%s,%d,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,40,40,%d,1\n"
            "Style: P,%s,66,&H0000F0FF,&H0000F0FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,8,30,30,260,1\n"
            "Style: C,%s,230,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,2,2,30,30,430,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
            % (W, H, CAPTION_FONT, CAPTION_SIZE, CAPTION_MARGIN_V, CAPTION_FONT, CAPTION_FONT))
    lines = []
    for i, (txt, st, en) in enumerate(all_words):
        end = all_words[i + 1][1] if i + 1 < len(all_words) and all_words[i + 1][1] - en < 0.4 else en + 0.15
        clean = txt.replace("{", "").replace("}", "").replace("\\", "")
        lines.append("Dialogue: 0,%s,%s,W,,0,0,0,,%s" % (ass_time(st), ass_time(end), clean))
    for (text, st, en, style) in overlays:
        lines.append("Dialogue: 1,%s,%s,%s,,0,0,0,,%s" % (ass_time(st), ass_time(en), style, text))
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")


def build_video(scenes, workdir, final_path, overlays=()):
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
    write_ass(all_words, os.path.join(workdir, "captions.ass"), overlays)
    run(["ffmpeg", "-y", "-v", "error", "-i", joined, "-vf",
         "ass=%s" % os.path.join(workdir, "captions.ass"),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy",
         "-movflags", "+faststart", final_path])


def add_overlays(scenes):
    """Compute on-screen puzzle text and countdown numbers (global times)."""
    events, t0 = [], 0.0
    width = 14 if LANG != "en" else 20
    for s in scenes:
        ov = s.get("overlay") or []
        if ov:
            lines = []
            for ln in ov:
                lines += textwrap.wrap(ln, width=width) or [ln]
            text = "\\N".join(l.replace("{", "").replace("}", "") for l in lines[:6])
            events.append((text, t0, t0 + s["dur"], "P"))
        if s.get("countdown"):
            start = t0 + s["voice_len"] + PAD
            for k in range(5):
                events.append((str(5 - k), start + k, start + k + 1, "C"))
        t0 += s["dur"]
    return events


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
# ============================ QUIZ MODE (missing-letters quiz) ============================
# No AI images needed: a dark background + on-screen text, a draining timer bar and an answer list.
QUIZ_ROUNDS = int(os.environ.get("QUIZ_ROUNDS", "10"))
QUIZ_TIMER = float(os.environ.get("QUIZ_TIMER", "4"))
QUIZ_REVEAL = 2.0
LATIN_FONT = "DejaVu Sans"

ITEMS = {
    "countries": ["Italy", "Spain", "Egypt", "Japan", "Norway", "Poland", "Mexico", "Greece", "Canada",
                  "Brazil", "France", "Turkey", "Kenya", "Sweden", "Cuba", "Portugal", "Germany",
                  "Ireland", "Nigeria", "Argentina", "Thailand", "Vietnam", "Morocco", "Ukraine",
                  "Austria", "Finland", "Denmark", "Hungary", "Iceland", "Jordan", "Nepal", "Peru",
                  "Chile", "Russia", "Australia", "Pakistan", "Indonesia", "Malaysia", "Singapore",
                  "Switzerland", "Bangladesh", "Colombia", "Ethiopia", "Ghana", "Oman", "Qatar"],
    "animals": ["Tiger", "Zebra", "Giraffe", "Elephant", "Monkey", "Rabbit", "Kangaroo", "Penguin",
                "Dolphin", "Panda", "Camel", "Donkey", "Leopard", "Cheetah", "Gorilla", "Ostrich",
                "Peacock", "Parrot", "Turtle", "Crocodile", "Buffalo", "Squirrel", "Hamster", "Eagle",
                "Falcon", "Rhino", "Koala", "Lion", "Hippo", "Jaguar", "Octopus", "Flamingo"],
    "fruits": ["Apple", "Mango", "Banana", "Orange", "Grapes", "Papaya", "Guava", "Cherry", "Lemon",
               "Melon", "Peach", "Plum", "Pineapple", "Coconut", "Apricot", "Avocado", "Pomegranate",
               "Strawberry", "Blueberry", "Watermelon", "Lychee", "Raspberry", "Tangerine", "Cranberry"],
    "cities": ["Paris", "London", "Tokyo", "Berlin", "Madrid", "Rome", "Cairo", "Moscow", "Sydney",
               "Dubai", "Mumbai", "Chicago", "Toronto", "Beijing", "Bangkok", "Vienna", "Athens",
               "Lisbon", "Istanbul", "Singapore", "Venice", "Prague", "Dublin", "Boston", "Seoul",
               "Delhi", "Kolkata", "Chennai", "Karachi", "Lahore", "Munich", "Zurich", "Geneva",
               "Miami", "Houston", "Seattle", "Havana", "Nairobi"],
}
CAT_WEIGHTS = {"countries": 4, "animals": 2, "fruits": 2, "cities": 2}

QT = {
    "en": {
        "header": {"countries": "GUESS COUNTRIES", "animals": "GUESS ANIMALS",
                   "fruits": "GUESS FRUITS", "cities": "GUESS CITIES"},
        "cat": {"countries": "countries", "animals": "animals", "fruits": "fruits", "cities": "cities"},
        "intro": "Can you name {n} {cat} from missing letters? Most people can't. You get {sec} seconds each. Let's go!",
        "outro": "How many did you get? Comment your score, and follow for a harder one tomorrow!",
        "title": "Can You Name {n} {cat} From Missing Letters?",
        "desc": "Guess {n} {cat} from the missing letters. Comment your score! #Shorts #quiz #brainteaser",
        "sub": "SUBSCRIBE", "hsize": 34,
    },
    "hi": {
        "header": {"countries": "देशों को पहचानो", "animals": "जानवर पहचानो",
                   "fruits": "फल पहचानो", "cities": "शहर पहचानो"},
        "cat": {"countries": "देशों", "animals": "जानवरों", "fruits": "फलों", "cities": "शहरों"},
        "intro": "क्या आप छूटे हुए अक्षरों से {n} {cat} के नाम बता सकते हैं? ज़्यादातर लोग नहीं बता पाते। हर सवाल के लिए {sec} सेकंड हैं। चलिए शुरू करें!",
        "outro": "आपने कितने सही बताए? कमेंट में अपना स्कोर लिखिए, और कल के कठिन क्विज़ के लिए फॉलो कीजिए!",
        "title": "क्या आप छूटे अक्षरों से {n} {cat} के नाम बता सकते हैं?",
        "desc": "छूटे अक्षरों से {n} {cat} के नाम पहचानिए। कमेंट में अपना स्कोर बताइए! #Shorts #quiz #हिंदी",
        "sub": "सब्सक्राइब", "hsize": 32,
    },
    "ml": {
        "header": {"countries": "രാജ്യങ്ങൾ ഊഹിക്കൂ", "animals": "മൃഗങ്ങളെ ഊഹിക്കൂ",
                   "fruits": "പഴങ്ങൾ ഊഹിക്കൂ", "cities": "നഗരങ്ങൾ ഊഹിക്കൂ"},
        "cat": {"countries": "രാജ്യങ്ങളുടെ", "animals": "മൃഗങ്ങളുടെ", "fruits": "പഴങ്ങളുടെ", "cities": "നഗരങ്ങളുടെ"},
        "intro": "വിട്ടുപോയ അക്ഷരങ്ങളിൽ നിന്ന് {n} {cat} പേരുകൾ പറയാമോ? മിക്കവർക്കും കഴിയില്ല. ഓരോ ചോദ്യത്തിനും {sec} സെക്കൻഡ് ഉണ്ട്. തുടങ്ങാം!",
        "outro": "നിങ്ങൾ എത്ര എണ്ണം ശരിയായി പറഞ്ഞു? കമന്റിൽ സ്കോർ എഴുതൂ, നാളെ കൂടുതൽ കടുപ്പമുള്ളതിനായി ഫോളോ ചെയ്യൂ!",
        "title": "വിട്ടുപോയ അക്ഷരങ്ങളിൽ നിന്ന് {n} {cat} പേരുകൾ പറയാമോ?",
        "desc": "വിട്ടുപോയ അക്ഷരങ്ങളിൽ നിന്ന് {n} {cat} പേരുകൾ കണ്ടെത്തൂ. കമന്റിൽ സ്കോർ പറയൂ! #Shorts #quiz #malayalam",
        "sub": "സബ്സ്ക്രൈബ്", "hsize": 28,
    },
}


def blank_word(word, nblank, rng):
    w = word.upper()
    nblank = max(1, min(nblank, len(w) - 2))
    vow = [i for i, c in enumerate(w) if c in "AEIOU" and i > 0]
    con = [i for i, c in enumerate(w) if c not in "AEIOU" and i > 0]
    rng.shuffle(vow)
    rng.shuffle(con)
    pick = (vow + con)[:nblank]
    return "".join("_" if i in pick else c for i, c in enumerate(w))


def translate_items(items, cat):
    """Spoken names in the video language (the on-screen puzzle stays in English letters)."""
    if LANG == "en":
        return list(items)
    try:
        res = gemini_json(
            "Translate these English words (%s) into %s, exactly as a native speaker would say them "
            "aloud, in native script. Return ONLY JSON {\"names\": [...]} with the same order and "
            "count.\n%s" % (cat, LC["name"], json.dumps(items)), rounds=2)
        names = [str(x) for x in res.get("names", [])]
        if len(names) == len(items):
            return names
    except Exception as e:
        log("  translation failed, speaking English names:", str(e)[:100])
    return list(items)


def rrect(w, h, r):
    return ("m %d 0 l %d 0 b %d 0 %d 0 %d %d l %d %d b %d %d %d %d %d %d l %d %d b 0 %d 0 %d 0 %d "
            "l 0 %d b 0 0 0 0 %d 0" % (r, w - r, w, w, w, r, w, h - r, w, h, w, h, w - r, h,
                                       r, h, h, h, h - r, r, r))


def make_bg(path):
    custom = ROOT / "assets" / "bg.jpg"
    if custom.exists():
        shutil.copy(str(custom), path)
        return
    from PIL import Image, ImageDraw, ImageFilter
    img = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):
        v = int(24 + 22 * y / H)
        px.line([(0, y), (W, y)], fill=(v, v, v + 4))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    cx, cy = W // 2, int(H * 0.46)
    for k in range(1, 7):                    # faint globe lines
        rx = int(70 * k)
        d.ellipse([cx - rx, cy - 330, cx + rx, cy + 330], outline=(170, 50, 60, 110), width=2)
    for k in range(-4, 5):
        yy = cy + k * 75
        d.line([(cx - 330, yy), (cx + 330, yy)], fill=(170, 50, 60, 90), width=2)
    ov = ov.filter(ImageFilter.GaussianBlur(1.2))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    img.save(path)


def _ev(layer, st, en, style, text):
    return "Dialogue: %d,%s,%s,%s,,0,0,0,,%s" % (layer, ass_time(st), ass_time(en), style, text)


def _pill(st, en, x, y, w, h, color, layer=0, extra=""):
    return _ev(layer, st, en, "D", "{\\an7\\pos(%d,%d)\\p1\\bord0\\shad0\\1c&H%s&%s}%s{\\p0}"
               % (x, y, color, extra, rrect(w, h, h // 2)))


def quiz_ass(path, total, header, sub, hsize, intro_words, outro_words, outro_start,
             rounds, items, intro_len, round_len):
    def style(name, font, size, color, align, outline, bold=-1):
        return ("Style: %s,%s,%d,%s,&H00FFFFFF,&H00000000,&H80000000,%d,0,0,0,100,100,0,0,1,%d,2,%d,20,20,20,1"
                % (name, font, size, color, bold, outline, align))
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n" % (W, H))
    head += "\n".join([
        style("D", LATIN_FONT, 20, "&H00FFFFFF", 7, 0),
        style("H", CAPTION_FONT, hsize, "&H00FFFFFF", 5, 0),
        style("P", LATIN_FONT, 96, "&H0000FFFF", 5, 6),
        style("I", CAPTION_FONT, 64, "&H0000FFFF", 5, 6),
        style("L", LATIN_FONT, 46, "&H00FFFFFF", 7, 4),
        style("S", CAPTION_FONT, 28, "&H00FFFFFF", 5, 0),
    ]) + "\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    ev = []
    ev.append(_pill(0, total, 110, 70, 500, 68, "4F11E0"))
    ev.append(_ev(1, 0, total, "H", "{\\pos(360,104)}%s" % header))
    n = len(items)

    def list_text(upto):
        out = []
        for k in range(n):
            out.append("%d) %s" % (k + 1, items[k] if k < upto else ""))
        return "\\N".join(out)

    def words_events(words, off, end):
        for i, (w, a, b) in enumerate(words):
            st = off + a
            nxt = off + words[i + 1][1] if i + 1 < len(words) else end
            ev.append(_ev(1, st, max(nxt, st + 0.05), "I",
                          "{\\pos(360,270)}%s" % w.replace("{", "").replace("}", "")))

    words_events(intro_words, 0.0, intro_len)
    ev.append(_ev(1, 0, intro_len, "L", "{\\pos(150,450)}%s" % list_text(0)))
    for i, (puz, ans) in enumerate(rounds):
        st = intro_len + i * round_len
        mid, en = st + QUIZ_TIMER, st + round_len
        ev.append(_ev(1, st, mid, "P", "{\\pos(360,270)\\fsp6}%s" % puz))
        ev.append(_ev(1, mid, en, "P", "{\\pos(360,270)\\fsp6\\1c&H50E050&}%s" % ans.upper()))
        ev.append(_pill(st, en, 160, 350, 400, 34, "E6D317", 0, "\\bord2\\3c&H000000&"))
        ev.append(_pill(st, mid, 160, 350, 400, 34, "0B9EF5", 1,
                        "\\bord2\\3c&H000000&\\fscx100\\t(0,%d,\\fscx0)" % int(QUIZ_TIMER * 1000)))
        ev.append(_ev(1, st, mid, "L", "{\\pos(150,450)}%s" % list_text(i)))
        ev.append(_ev(1, mid, en, "L", "{\\pos(150,450)}%s" % list_text(i + 1)))
        if i in (2, 6):
            ev.append(_pill(st, en, 430, 760, 220, 58, "4F11E0", 0))
            ev.append(_ev(1, st, en, "S", "{\\pos(540,789)}%s" % sub))
    ev.append(_ev(1, outro_start, total, "L", "{\\pos(150,450)}%s" % list_text(n)))
    words_events(outro_words, outro_start, total)
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(ev) + "\n")


def _wav(cmd_in, out, extra_af=None, dur=None):
    cmd = ["ffmpeg", "-y", "-v", "error"] + cmd_in
    if extra_af:
        cmd += ["-af", extra_af]
    if dur:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out]
    run(cmd)


def build_quiz(work):
    """Returns (meta dict, final video path)."""
    cat = random.choices(list(CAT_WEIGHTS), weights=list(CAT_WEIGHTS.values()))[0]
    items = random.sample(ITEMS[cat], QUIZ_ROUNDS)
    L = QT[LANG]
    rng = random.Random()
    log("1/3 quiz category: %s -> %s" % (cat, ", ".join(items)))
    spoken = translate_items(items, cat)
    rounds = []
    for i, it in enumerate(items):
        nb = 1 if i < 3 else (2 if i < 7 else 3)
        rounds.append((blank_word(it, nb, rng), it))

    intro_text = L["intro"].format(n=QUIZ_ROUNDS, cat=L["cat"][cat], sec=int(QUIZ_TIMER))
    outro_text = L["outro"]
    round_len = QUIZ_TIMER + QUIZ_REVEAL

    log("2/3 voice and sounds")
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=1100:duration=0.06",
         "-af", "afade=t=out:st=0.03:d=0.03,volume=0.5,aformat=sample_rates=44100:channel_layouts=stereo",
         os.path.join(work, "tick.wav")])
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=1320:duration=0.5",
         "-af", "afade=t=out:st=0.05:d=0.45,volume=0.6,aformat=sample_rates=44100:channel_layouts=stereo",
         os.path.join(work, "ding.wav")])

    voice_ok = True

    def speak(text, name):
        nonlocal voice_ok
        if not voice_ok:
            return None, None
        mp3 = os.path.join(work, name + ".mp3")
        try:
            words = make_voice(text, mp3)
            return mp3, words
        except Exception as e:
            log("  voice unavailable, building the quiz without narration:", str(e)[:100])
            voice_ok = False
            return None, None

    segs = []
    # intro
    mp3, words = speak(intro_text, "intro")
    if mp3:
        intro_len = probe_duration(mp3) + 0.5
        _wav(["-i", mp3], os.path.join(work, "seg_intro.wav"), "apad", intro_len)
    else:
        intro_len = 6.0
        words = spread_words(intro_text, intro_len)
        _wav(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"], os.path.join(work, "seg_intro.wav"), None, intro_len)
    intro_words = words
    segs.append(os.path.join(work, "seg_intro.wav"))

    # rounds: ticks while the timer runs, a ding + the spoken answer at the reveal
    nticks = max(1, int(QUIZ_TIMER))
    for i in range(QUIZ_ROUNDS):
        vmp3, _ = speak(spoken[i], "r%02d" % i)
        ins = ["-f", "lavfi", "-t", "%.2f" % round_len, "-i", "anullsrc=r=44100:cl=stereo",
               "-i", os.path.join(work, "tick.wav"), "-i", os.path.join(work, "ding.wav")]
        parts = ["[1:a]asplit=%d%s" % (nticks, "".join("[k%d]" % k for k in range(nticks))) if nticks > 1 else "[1:a]anull[k0]"]
        labels = ["[0:a]"]
        for k in range(nticks):
            ms = int((0.4 + k) * 1000)
            parts.append("[k%d]adelay=%d|%d[t%d]" % (k, ms, ms, k))
            labels.append("[t%d]" % k)
        dms = int(QUIZ_TIMER * 1000)
        parts.append("[2:a]adelay=%d|%d[dg]" % (dms, dms))
        labels.append("[dg]")
        if vmp3:
            ins += ["-i", vmp3]
            vms = dms + 120
            parts.append("[3:a]adelay=%d|%d,volume=1.6[vc]" % (vms, vms))
            labels.append("[vc]")
        parts.append("%samix=inputs=%d:duration=longest:normalize=0[o]" % ("".join(labels), len(labels)))
        out = os.path.join(work, "seg_r%02d.wav" % i)
        run(["ffmpeg", "-y", "-v", "error"] + ins + ["-filter_complex", ";".join(parts), "-map", "[o]",
             "-t", "%.3f" % round_len, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out])
        segs.append(out)
        log("   round %d/%d  %s -> %s" % (i + 1, QUIZ_ROUNDS, rounds[i][0], rounds[i][1]))

    # outro
    mp3, words = speak(outro_text, "outro")
    if mp3:
        outro_len = probe_duration(mp3) + 0.6
        _wav(["-i", mp3], os.path.join(work, "seg_outro.wav"), "apad", outro_len)
    else:
        outro_len = 6.0
        words = spread_words(outro_text, outro_len)
        _wav(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"], os.path.join(work, "seg_outro.wav"), None, outro_len)
    outro_words = words
    segs.append(os.path.join(work, "seg_outro.wav"))

    lst = os.path.join(work, "audio.txt")
    with open(lst, "w") as f:
        for sgm in segs:
            f.write("file '%s'\n" % os.path.basename(sgm))
    audio = os.path.join(work, "audio.wav")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", audio])

    log("3/3 rendering video")
    total = intro_len + QUIZ_ROUNDS * round_len + outro_len
    bg = os.path.join(work, "bg.png")
    make_bg(bg)
    quiz_ass(os.path.join(work, "quiz.ass"), total, L["header"][cat], L["sub"], L["hsize"],
             intro_words, outro_words, intro_len + QUIZ_ROUNDS * round_len, rounds, items,
             intro_len, round_len)
    captioned = os.path.join(work, "quiz_video.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", bg, "-i", audio,
         "-vf", "ass=%s,format=yuv420p" % os.path.join(work, "quiz.ass"),
         "-t", "%.3f" % total, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", captioned])
    final = os.path.join(OUT, "video.mp4")
    add_music(captioned, final)

    catname = L["cat"][cat]
    meta = {"title": L["title"].format(n=QUIZ_ROUNDS, cat=catname),
            "description": L["desc"].format(n=QUIZ_ROUNDS, cat=catname),
            "tags": ["quiz", "guess", "brain teaser", "shorts", cat, "trivia", "challenge"],
            "recap": "quiz: %s (%s)" % (cat, ", ".join(items))}
    return meta, final


def main():
    if not API_KEY:
        sys.exit("Set GEMINI_KEY")
    if not DRY_RUN:
        try:
            yt_access_token()          # check the YouTube login BEFORE spending time on the video
            log("YouTube login OK")
        except Exception as e:
            log(str(e))
            sys.exit(3)
    topic = " ".join(sys.argv[1:]).strip() or os.environ.get("TOPIC", "").strip()
    story = load_story()
    n = len(story["episodes"]) + 1
    work = os.path.join(OUT, "work")
    plan = os.path.join(work, "plan.json")
    if VIDEO_MODE == "quiz":
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        meta, final = build_quiz(work)
        with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        log("built quiz episode %d: %s (%.1fs)" % (n, final, probe_duration(final)))
        if DRY_RUN:
            log("DRY_RUN=1: skipping upload and story save")
            return
        video_id = upload(final, meta)
        log("uploaded https://youtu.be/%s" % video_id)
        story["episodes"].append({"n": n, "lang": LANG, "title": meta["title"], "recap": meta["recap"]})
        STORY_FILE.write_text(json.dumps(story, indent=2))
        return
    if os.environ.get("RESUME") == "1" and os.path.exists(plan):
        log("1/4 RESUME: reusing the script and images from the previous attempt")
        with open(plan, encoding="utf-8") as f:
            data = json.load(f)
    else:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        log("1/4 writing script for:", topic)
        data = write_script(topic, story)
        with open(plan, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    scenes = data["scenes"]
    log("   %d scenes, title: %s" % (len(scenes), data.get("title")))

    log("2/4 generating images")
    prev = None
    reused = 0
    for i, s in enumerate(scenes):
        path = os.path.join(work, "img_%02d.png" % i)
        if os.path.exists(path) and os.path.getsize(path) > 5000:
            s["img"] = path
            prev = path
            log("   image %d/%d (kept from before)" % (i + 1, len(scenes)))
            continue
        ok = gen_image(s, i, path)
        if not ok:
            if prev:
                log("   scene %d: image failed, reusing previous" % i)
                shutil.copy(prev, path)
                reused += 1
                if reused >= 3:
                    log("Too many images failed (no working image source). Not building a bad video.")
                    log("Fix: add CF_ACCOUNT_ID + CF_API_TOKEN secrets (free Cloudflare Workers AI) "
                        "or enable billing for Gemini images.")
                    sys.exit(4)
            else:
                sys.exit("First image failed; check API key / image model access.")
        s["img"] = path
        prev = path
        log("   image %d/%d" % (i + 1, len(scenes)))
        time.sleep(1.5)

    log("3/4 generating voice")
    make_all_voice(scenes, work)

    for s in scenes:        # countdown scenes get 5 extra silent seconds for the viewer to think
        s["voice_len"] = probe_duration(s["mp3"])
        s["silence"] = 5.0 if s.get("countdown") else 0.0
        s["dur"] = s["voice_len"] + PAD + s["silence"]

    log("4/4 rendering video")
    captioned = os.path.join(work, "captioned.mp4")
    build_video(scenes, work, captioned, add_overlays(scenes))
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
