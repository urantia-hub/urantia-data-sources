"""
Generate TTS audio for the Urantia Book using OpenAI's TTS API.

Parameterized version of the original generate_voice.py with improvements
for professional audiobook narration (Spotify distribution).

Key improvements over v1:
  - CLI arguments (model, voice, speed, paper range, etc.)
  - Opus intermediate format (avoids double MP3 compression)
  - Automatic MP3 conversion for CDN upload
  - Enhanced text preprocessing for natural narration
  - Progress tracking with cost monitoring
  - Dry-run mode for segment counting

Usage:
  # Test on a single paragraph
  python generate_voice_v2.py --start-paper 3 --end-paper 3 --limit 1

  # Dry run to count segments
  python generate_voice_v2.py --dry-run

  # Full generation with onyx voice
  python generate_voice_v2.py --voice onyx

  # Generate MP3 instead of opus (simpler, no ffmpeg needed)
  python generate_voice_v2.py --response-format mp3
"""

import openai
from openai import OpenAI
import os
import json
import time
import subprocess
import argparse
import re
import inflect
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime

# ---------------------------------------------------------------------------
# CLI Arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Generate TTS audio for the Urantia Book")
parser.add_argument("--model", default="gpt-4o-mini-tts", choices=["tts-1", "tts-1-hd", "gpt-4o-mini-tts"])
parser.add_argument("--voice", default="cedar", choices=["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"])
parser.add_argument("--start-paper", type=int, default=0, help="First paper to process (0-196)")
parser.add_argument("--end-paper", type=int, default=196, help="Last paper to process (0-196)")
parser.add_argument("--speed", type=float, default=1.0, help="Narration speed (0.25-4.0)")
parser.add_argument("--response-format", default="opus", choices=["mp3", "opus", "aac", "flac", "wav", "pcm"],
                    help="Audio format from OpenAI API (opus recommended for intermediates)")
parser.add_argument("--convert-mp3", action="store_true", default=True,
                    help="Convert opus/flac to MP3 after generation (default: true)")
parser.add_argument("--no-convert-mp3", dest="convert_mp3", action="store_false",
                    help="Skip MP3 conversion")
parser.add_argument("--instructions", type=str,
                    default="Narrate in a calm, measured, authoritative tone suitable for a philosophical audiobook. Speak clearly with natural pacing. Do not rush. Maintain a warm but reverent quality throughout.",
                    help="Voice instructions for gpt-4o-mini-tts (ignored for tts-1/tts-1-hd)")
parser.add_argument("--no-instructions", dest="instructions", action="store_const", const=None,
                    help="Disable voice instructions")
parser.add_argument("--delay", type=float, default=0.0, help="Seconds to wait between API requests")
parser.add_argument("--dry-run", action="store_true", help="Count segments without generating audio")
parser.add_argument("--limit", type=int, default=0, help="Max segments to generate (0 = unlimited)")
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
p = inflect.engine()

# Load .env — check multiple locations
repo_root = Path(__file__).parent.parent
env_candidates = [
    repo_root / '.env',
    repo_root.parent / 'urantia-hub-api' / '.env',
    repo_root.parent / 'urantia-dev-api' / '.env',
]
for env_path in env_candidates:
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        if os.getenv("OPENAI_API_KEY"):
            print(f"Loaded OPENAI_API_KEY from {env_path}")
            break

if os.getenv("OPENAI_API_KEY") is None:
    print("OPENAI_API_KEY environment variable not found")
    print(f"Checked: {[str(p) for p in env_candidates]}")
    exit(1)

client = OpenAI()
print(f"OPENAI_API_KEY is ready")
print(f"Config: model={args.model}, voice={args.voice}, speed={args.speed}, format={args.response_format}")
print(f"Papers: {args.start_paper} to {args.end_paper}")

# Paths relative to urantia-data-sources/
json_dir = repo_root / 'data' / 'json' / 'eng'
output_dir = repo_root / 'data' / 'audio' / 'eng' / 'paragraphs' / args.voice
output_dir.mkdir(parents=True, exist_ok=True)

# File extension for the raw API output
ext = args.response_format if args.response_format != "pcm" else "raw"

# ---------------------------------------------------------------------------
# Text Preprocessing
# ---------------------------------------------------------------------------

def number_to_words(text):
    """Convert numbers like 1,500,000 to words."""
    def convert(match):
        number = match.group().replace(',', '')
        return p.number_to_words(number)
    return re.sub(r'\b\d[\d,]*\b', convert, text)


def roman_to_int(roman):
    roman_numerals = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    num = 0
    prev_value = 0
    for letter in reversed(roman.upper()):
        value = roman_numerals[letter]
        if value < prev_value:
            num -= value
        else:
            num += value
        prev_value = value
    return num


def number_to_words_lookup(num):
    number_dict = {
        1: "Number One", 2: "Number Two", 3: "Number Three", 4: "Number Four",
        5: "Number Five", 6: "Number Six", 7: "Number Seven", 8: "Number Eight",
        9: "Number Nine", 10: "Number Ten", 11: "Number Eleven", 12: "Number Twelve",
        13: "Number Thirteen", 14: "Number Fourteen", 15: "Number Fifteen",
        16: "Number Sixteen", 17: "Number Seventeen", 18: "Number Eighteen",
        19: "Number Nineteen", 20: "Number Twenty",
    }
    return number_dict.get(num, str(num))


def preprocess_for_narration(text):
    """Additional preprocessing for natural audiobook narration."""
    # Em dashes to ellipsis pauses (OpenAI TTS treats ... as a breath pause)
    text = text.replace(" — ", " ... ")
    text = text.replace("—", " ... ")

    # ALL CAPS words (3+ chars) to Title Case to prevent TTS from spelling them out
    # e.g., DEITY -> Deity, ABSOLUTE -> Absolute
    text = re.sub(r'\b([A-Z]{3,})\b', lambda m: m.group().title(), text)

    # Soften parentheticals with commas for natural intonation
    text = re.sub(r'\(([^)]+)\)', r', \1,', text)

    return text


def process_section_title(title):
    """Process section titles: convert Roman numeral prefixes to words."""
    match = re.match(r'^([IVXLCDM]+)\.\s*', title)
    if match:
        roman_numeral = match.group(1)
        number = roman_to_int(roman_numeral)
        words = number_to_words_lookup(number)
        title = re.sub(r'^[IVXLCDM]+\.\s*', f"{words}... ", title)
    return process_text(title)


def process_text(text):
    """Process paragraph text: numbered lists and number-to-words conversion."""
    # If text starts with a number followed by `.` or `)`, replace with words
    match = re.match(r'^(\d+)[.)]\s*', text)
    if match:
        number = int(match.group(1))
        text = re.sub(r'^\d+[.)]\s*', f"{number_to_words_lookup(number)}... ", text)

    # Convert remaining numbers to words
    text = number_to_words(text)

    # Apply narration-specific preprocessing
    text = preprocess_for_narration(text)

    return text


# ---------------------------------------------------------------------------
# Progress Tracking
# ---------------------------------------------------------------------------

progress_file = output_dir / f"{args.voice}_progress.json"


def load_progress():
    if progress_file.exists():
        return json.loads(progress_file.read_text())
    return {"completed": [], "total_chars": 0, "total_segments": 0, "started_at": datetime.now().isoformat()}


def save_progress(progress, global_id, char_count):
    progress["completed"].append({
        "id": global_id,
        "chars": char_count,
        "ts": datetime.now().isoformat(),
    })
    progress["total_chars"] += char_count
    progress["total_segments"] += 1
    progress_file.write_text(json.dumps(progress, indent=2))


# ---------------------------------------------------------------------------
# Audio Generation
# ---------------------------------------------------------------------------

def retry_with_exponential_backoff(func, initial_delay=1, max_retries=10, exponential_base=2):
    def wrapper(*args_inner, **kwargs):
        delay = initial_delay
        for attempt in range(max_retries):
            try:
                return func(*args_inner, **kwargs)
            except Exception as e:
                print(f"  Error: {e}. Retrying in {delay}s...")
                time.sleep(delay)
                delay *= exponential_base
        raise Exception("Maximum retries exceeded")
    return wrapper


@retry_with_exponential_backoff
def generate_audio(text, output_path):
    """Call OpenAI TTS API and save the audio file."""
    kwargs = dict(
        model=args.model,
        voice=args.voice,
        input=text,
        speed=args.speed,
        response_format=args.response_format,
    )
    # instructions only supported by gpt-4o-mini-tts
    if args.instructions and args.model == "gpt-4o-mini-tts":
        kwargs["instructions"] = args.instructions
    response = client.audio.speech.create(**kwargs)
    with open(output_path, 'wb') as f:
        f.write(response.content)
    return True


def convert_to_mp3(input_path, output_path):
    """Convert audio file to MP3 using ffmpeg (192kbps, 44.1kHz, mono)."""
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-ar", "44100",
        "-ac", "1",
        "-b:a", "192k",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ffmpeg error: {result.stderr[:200]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Cost Warning
# ---------------------------------------------------------------------------

if not args.dry_run:
    print("\nWarning: Running this script will incur OpenAI API costs.")
    print(f"Estimated: ~$0.030 per 1,000 characters for {args.model}")
    print("Do you want to proceed? (y/N)")
    user_input = input().strip().lower()
    if user_input != "y":
        print("Operation aborted by the user.")
        exit(0)

# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

progress = load_progress()
files_created = 0
files_existing = 0
files_failed = 0
total_chars = 0
dry_run_segments = 0

for i in range(args.start_paper, args.end_paper + 1):
    json_path = json_dir / f"{i:03}.json"

    if not json_path.exists():
        print(f"File not found: {json_path}")
        files_failed += 1
        continue

    with open(json_path, 'r', encoding='utf-8') as file:
        data = json.load(file)

    paper_segments = 0

    for item in data:
        # Check limit
        if args.limit > 0 and files_created >= args.limit:
            break

        text_to_read = None

        if item['type'] == 'part' and item.get('partTitle') and item.get('partSponsorship'):
            text_to_read = f"Part {item['partId']} - {item['partTitle']} - {item['partSponsorship']}"
        elif item['type'] == 'paper' and item['paperId'] != '0':
            text_to_read = f"Paper {item['paperId']} - {item['paperTitle']}"
        elif item['type'] == 'paper' and item['paperId'] == '0':
            text_to_read = "Foreword"
        elif item['type'] == 'section' and item.get('sectionTitle'):
            section_title = process_section_title(item['sectionTitle'])
            text_to_read = section_title
        elif item['type'] == 'paragraph':
            text_to_read = process_text(item['text'])

        if text_to_read:
            global_id = item['globalId']

            if args.dry_run:
                dry_run_segments += 1
                total_chars += len(text_to_read)
                continue

            # Primary file: opus (or whatever response_format is)
            primary_filename = f"{args.model}-{args.voice}-{global_id}.{ext}"
            primary_path = output_dir / primary_filename

            # MP3 conversion path
            mp3_filename = f"{args.model}-{args.voice}-{global_id}.mp3"
            mp3_path = output_dir / mp3_filename

            # Skip if primary file OR mp3 already exists
            if primary_path.exists() or mp3_path.exists():
                files_existing += 1
                continue

            print(f"Generating {primary_filename}...")
            try:
                if generate_audio(text_to_read, primary_path):
                    files_created += 1
                    paper_segments += 1
                    save_progress(progress, global_id, len(text_to_read))

                    # Convert to MP3 if needed
                    if args.convert_mp3 and args.response_format != "mp3":
                        if convert_to_mp3(primary_path, mp3_path):
                            print(f"  ✅ {primary_filename} + {mp3_filename}")
                        else:
                            print(f"  ✅ {primary_filename} (mp3 conversion failed)")
                    else:
                        print(f"  ✅ {primary_filename}")

                    # Delay between requests if configured
                    if args.delay > 0:
                        time.sleep(args.delay)
            except Exception as e:
                print(f"  ❌ Failed: {e}")
                files_failed += 1

    if not args.dry_run and paper_segments > 0:
        print(f"Paper {i}: {paper_segments} segments generated")

    if args.limit > 0 and files_created >= args.limit:
        print(f"Limit of {args.limit} segments reached.")
        break

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

# Cost per 1K chars by model
COST_PER_1K = {
    "tts-1": 0.015,
    "tts-1-hd": 0.030,
    "gpt-4o-mini-tts": 0.003,
}
cost_rate = COST_PER_1K.get(args.model, 0.030)

if args.dry_run:
    estimated_cost = (total_chars / 1000) * cost_rate
    print(f"\n--- Dry Run Summary ---")
    print(f"Segments: {dry_run_segments}")
    print(f"Total characters: {total_chars:,}")
    print(f"Estimated cost ({args.model} @ ${cost_rate}/1K chars): ${estimated_cost:.2f}")
else:
    print(f"\n--- Generation Summary ---")
    print(f"Created: {files_created}")
    print(f"Already existed: {files_existing}")
    print(f"Failed: {files_failed}")
    if progress["total_chars"] > 0:
        cost = (progress["total_chars"] / 1000) * cost_rate
        print(f"Characters this session: {progress['total_chars']:,}")
        print(f"Estimated cost this session: ${cost:.2f}")
