"""
Combine individual TTS audio segments into per-paper audiobook chapters.

Uses ffmpeg directly (no pydub) to avoid double MP3 compression.
Pipeline: opus segments → ffmpeg concat → single MP3 encode with loudness normalization.

Usage:
  # Combine a single paper (Paper 3)
  python combine_audio_v2.py --voice onyx --start-paper 3 --end-paper 3

  # Combine all papers
  python combine_audio_v2.py --voice onyx

  # Use mp3 source files instead of opus
  python combine_audio_v2.py --voice onyx --source-format mp3
"""

import os
import subprocess
import argparse
import json
import re
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# CLI Arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Combine TTS segments into per-paper audiobook chapters")
parser.add_argument("--model", default="gpt-4o-mini-tts", choices=["tts-1", "tts-1-hd", "gpt-4o-mini-tts"])
parser.add_argument("--voice", default="cedar", choices=["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"])
parser.add_argument("--start-paper", type=int, default=0, help="First paper to combine (0-196)")
parser.add_argument("--end-paper", type=int, default=196, help="Last paper to combine (0-196)")
parser.add_argument("--source-format", default="opus", choices=["opus", "mp3"],
                    help="Source audio format to read (default: opus)")
parser.add_argument("--normalize", action="store_true", default=True,
                    help="Apply loudness normalization (default: true)")
parser.add_argument("--no-normalize", dest="normalize", action="store_false")
parser.add_argument("--target-lufs", type=float, default=-16.0, help="Target loudness in LUFS")
parser.add_argument("--target-tp", type=float, default=-1.5, help="Target true peak in dBTP")
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Constants — silence durations in seconds
# ---------------------------------------------------------------------------
LATENCY_S = 0.250          # Between paragraphs
TITLE_LATENCY_S = 2.0      # After paper title
SECTION_BEFORE_S = 0.750    # Before section title
SECTION_AFTER_S = 1.0       # After section title
START_SILENCE_S = 0.750     # Start of chapter
END_SILENCE_S = 2.0         # End of chapter
BITRATE = "192k"
SAMPLE_RATE = 44100

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
repo_root = Path(__file__).parent.parent
segments_dir = repo_root / 'data' / 'audio' / 'eng' / 'paragraphs' / args.voice
output_dir = repo_root / 'data' / 'audio' / 'eng' / 'audiobook' / args.voice / 'papers'
output_dir.mkdir(parents=True, exist_ok=True)
json_dir = repo_root / 'data' / 'json' / 'eng'

# Load paper titles from JSON
PAPER_TITLES = {}
for i in range(197):
    json_path = json_dir / f"{i:03}.json"
    if json_path.exists():
        with open(json_path, 'r') as f:
            data = json.load(f)
        for item in data:
            if item['type'] == 'paper':
                if item['paperId'] == '0':
                    PAPER_TITLES['0'] = 'Foreword'
                else:
                    PAPER_TITLES[item['paperId']] = f"Paper {item['paperId']} - {item['paperTitle']}"
                break

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_global_id(filename):
    """Extract globalId from filename like tts-1-hd-onyx-1:3.0.1.opus"""
    prefix = f"{args.model}-{args.voice}-"
    if not filename.startswith(prefix):
        return None
    remainder = filename[len(prefix):]
    global_id = remainder.rsplit('.', 1)[0]
    return global_id


def get_paper_id(global_id):
    """Extract paper ID from globalId like 1:3.0.1 -> 3"""
    parts = global_id.split(":")
    if len(parts) == 2:
        return parts[1].split(".")[0]
    return None


def get_sort_key(global_id):
    """Create a sortable key from globalId like 1:3.0.1 -> 001:003.000.001"""
    part_id, rest = global_id.split(":")
    components = rest.split(".")
    padded = []
    for c in components:
        padded.append("000" if c == "-" else c.zfill(3))
    return f"{part_id.zfill(3)}:{'.'.join(padded)}"


def is_paper_title(global_id):
    """Check if globalId is a paper title: pattern like 1:3.-.-"""
    parts = global_id.split(":")
    if len(parts) == 2:
        components = parts[1].split(".")
        return len(components) == 3 and components[1] == "-" and components[2] == "-"
    return False


def is_section_title(global_id):
    """Check if globalId is a section title: pattern like 1:3.1.-"""
    parts = global_id.split(":")
    if len(parts) == 2:
        components = parts[1].split(".")
        return len(components) == 3 and components[1] != "-" and components[2] == "-"
    return False


def generate_silence_file(duration_s, sample_rate, output_path, fmt="opus"):
    """Generate a silence file in the same format as source audio."""
    if fmt == "opus":
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"anullsrc=r=48000:cl=mono",
            "-t", str(duration_s),
            "-c:a", "libopus", "-b:a", "64k",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t", str(duration_s),
            "-ar", str(sample_rate), "-ac", "1",
            "-b:a", "192k",
            str(output_path),
        ]
    subprocess.run(cmd, capture_output=True, check=True)


def measure_loudness(input_path, target_lufs, target_tp):
    """Pass 1 of two-pass loudnorm: measure the file."""
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path), "-af",
        f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11:print_format=json",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    json_match = re.search(r'\{[^{}]+\}', result.stderr, re.DOTALL)
    if not json_match:
        return None
    return json.loads(json_match.group())


def apply_loudnorm(input_path, output_path, measured, target_lufs, target_tp):
    """Pass 2 of two-pass loudnorm: apply with measured values."""
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path), "-af",
        f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"linear=true",
        "-ar", str(SAMPLE_RATE), "-ac", "1",
        "-b:a", BITRATE, "-id3v2_version", "3",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def add_id3_tags(mp3_path, paper_title, track_num):
    """Add ID3 tags to the final MP3 using ffmpeg."""
    tmp = str(mp3_path) + ".tagged.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", str(mp3_path),
        "-codec", "copy",
        "-id3v2_version", "3",
        "-metadata", f"album=The Urantia Book",
        "-metadata", f"artist=UrantiaHub",
        "-metadata", f"title={paper_title}",
        "-metadata", f"track={track_num}/197",
        "-metadata", f"genre=Audiobook",
        "-metadata", f"date=2026",
        tmp,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        os.replace(tmp, str(mp3_path))
        return True
    else:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print(f"Combining {args.voice} segments ({args.source_format}) into audiobook chapters")
print(f"Source: {segments_dir}")
print(f"Output: {output_dir}")
print(f"Papers: {args.start_paper} to {args.end_paper}")
print(f"Normalization: {'ON (-' + str(abs(args.target_lufs)) + ' LUFS)' if args.normalize else 'OFF'}")
print(f"Pipeline: opus → ffmpeg concat → single MP3 encode (no double compression)")

# Collect all segment files
all_files = list(segments_dir.glob(f"{args.model}-{args.voice}-*.{args.source_format}"))

if not all_files:
    print(f"No {args.source_format} files found in {segments_dir}")
    exit(1)

# Group by paper
papers = {}
for filepath in all_files:
    global_id = get_global_id(filepath.name)
    if not global_id:
        continue
    paper_id = get_paper_id(global_id)
    if paper_id is None:
        continue
    if paper_id not in papers:
        papers[paper_id] = []
    papers[paper_id].append((global_id, filepath))

# Pre-generate silence files in same format as source (avoids concat format mismatch)
silence_dir = tempfile.mkdtemp(prefix="urantia_silence_")
silence_ext = args.source_format  # opus or mp3
silence_files = {}
for label, dur in [
    ("start", START_SILENCE_S),
    ("end", END_SILENCE_S),
    ("paragraph", LATENCY_S),
    ("title_after", TITLE_LATENCY_S),
    ("section_before", SECTION_BEFORE_S),
    ("section_after", SECTION_AFTER_S),
]:
    path = os.path.join(silence_dir, f"silence_{label}.{silence_ext}")
    generate_silence_file(dur, SAMPLE_RATE, path, fmt=silence_ext)
    silence_files[label] = path

# Process requested papers
combined_count = 0
for paper_id_int in range(args.start_paper, args.end_paper + 1):
    paper_id = str(paper_id_int)
    if paper_id not in papers:
        continue

    segments = papers[paper_id]
    segments.sort(key=lambda x: get_sort_key(x[0]))

    paper_title = PAPER_TITLES.get(paper_id, f"Paper {paper_id}")
    padded_id = paper_id.zfill(3)
    output_filename = f"{padded_id}--{paper_title}.mp3"
    output_path = output_dir / output_filename

    print(f"\nCombining Paper {paper_id}: {paper_title} ({len(segments)} segments)")

    # Build ffmpeg concat list
    # Each entry is a file path — ffmpeg concat demuxer will decode and concatenate
    concat_entries = []

    # Start silence
    concat_entries.append(silence_files["start"])

    for global_id, filepath in segments:
        if is_paper_title(global_id):
            concat_entries.append(str(filepath))
            concat_entries.append(silence_files["title_after"])
        elif is_section_title(global_id):
            concat_entries.append(silence_files["section_before"])
            concat_entries.append(str(filepath))
            concat_entries.append(silence_files["section_after"])
        else:
            concat_entries.append(str(filepath))
            concat_entries.append(silence_files["paragraph"])

    # End silence
    concat_entries.append(silence_files["end"])

    # Write concat list file with absolute paths
    # Absolute paths avoid ffmpeg misinterpreting ':' in filenames as protocol prefixes
    concat_list_path = os.path.join(silence_dir, f"concat_{paper_id}.txt")
    with open(concat_list_path, 'w') as f:
        for entry in concat_entries:
            abs_path = os.path.abspath(entry)
            escaped = abs_path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    # Single ffmpeg command: concat → resample → mono → MP3
    # This is ONE encode from the original opus sources
    if args.normalize:
        # Step 1: Concat to intermediate WAV (lossless)
        intermediate_wav = os.path.join(silence_dir, f"paper_{paper_id}.wav")
        cmd_concat = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list_path,
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            intermediate_wav,
        ]
        result = subprocess.run(cmd_concat, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ❌ Concat failed: {result.stderr[:300]}")
            continue

        # Step 2: Measure loudness
        measured = measure_loudness(intermediate_wav, args.target_lufs, args.target_tp)
        if not measured:
            print(f"  ⚠️  Loudness measurement failed, encoding without normalization")
            cmd_encode = [
                "ffmpeg", "-y", "-i", intermediate_wav,
                "-ar", str(SAMPLE_RATE), "-ac", "1",
                "-b:a", BITRATE,
                str(output_path),
            ]
            subprocess.run(cmd_encode, capture_output=True, check=True)
        else:
            # Step 3: Apply loudnorm + encode to MP3 in one pass
            if not apply_loudnorm(intermediate_wav, str(output_path), measured,
                                  args.target_lufs, args.target_tp):
                print(f"  ❌ Normalization failed")
                continue

        os.unlink(intermediate_wav)
    else:
        # No normalization: concat directly to MP3
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list_path,
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-b:a", BITRATE,
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ❌ Failed: {result.stderr[:300]}")
            continue

    # Add ID3 tags
    add_id3_tags(output_path, paper_title, paper_id_int + 1)

    # Get file info
    size_mb = output_path.stat().st_size / (1024 * 1024)
    probe_cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(output_path)]
    duration_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    duration_min = float(duration_result.stdout.strip()) / 60 if duration_result.stdout.strip() else 0

    norm_label = f"normalized to {args.target_lufs} LUFS" if args.normalize else "no normalization"
    print(f"  ✅ {output_filename} ({duration_min:.1f} min, {size_mb:.0f}MB, {norm_label})")

    combined_count += 1

# Cleanup silence files
import shutil
shutil.rmtree(silence_dir, ignore_errors=True)

print(f"\n--- Done: {combined_count} papers combined ---")
