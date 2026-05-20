#!/usr/bin/env python3
"""Fetch a transcript from a Loom video URL.

Uses Loom's public GraphQL endpoint to retrieve auto-generated transcripts.
Works for public videos without authentication. For private videos, a password
can be supplied via --password.

Output: plain text transcript (VTT timestamps stripped) written to stdout
or to a file via --output.
"""

import argparse
import json
import re
import sys

import requests


def extract_video_id(url):
    """Extract the 32-char hex video ID from a Loom URL."""
    match = re.search(r"loom\.com/(?:share|embed)/([a-f0-9]{32})", url)
    if not match:
        sys.exit(f"Error: could not extract video ID from URL: {url}")
    return match.group(1)


def fetch_transcript_urls(video_id, password=None):
    """Query Loom GraphQL API for transcript source URLs."""
    query = """
    query FetchVideoTranscript($videoId: ID!, $password: String) {
      fetchVideoTranscript(videoId: $videoId, password: $password) {
        ... on VideoTranscriptDetails {
          id
          video_id
          source_url
          captions_source_url
        }
        ... on GenericError {
          message
        }
      }
    }
    """
    resp = requests.post(
        "https://www.loom.com/graphql",
        json={"query": query, "variables": {"videoId": video_id, "password": password}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()

    result = data.get("data", {}).get("fetchVideoTranscript", {})
    if "message" in result:
        sys.exit(f"Loom API error: {result['message']}")
    if not result.get("captions_source_url") and not result.get("source_url"):
        sys.exit("Error: no transcript available for this video.")
    return result


def fetch_vtt(url):
    """Download VTT captions and return raw text."""
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.text


def vtt_to_plain_text(vtt_text):
    """Convert VTT captions to clean plain text, stripping timestamps and tags."""
    lines = vtt_text.split("\n")
    text_lines = []
    prev_line = ""

    for line in lines:
        stripped = line.strip()
        # Skip header, sequence numbers, timestamps, empty lines
        if stripped == "WEBVTT":
            continue
        if re.match(r"^\d+$", stripped):
            continue
        if re.match(r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->", stripped):
            continue
        if not stripped:
            continue

        # Strip VTT voice tags like <v 0>...</v>
        clean = re.sub(r"</?v[^>]*>", "", stripped).strip()
        if not clean:
            continue

        # Deduplicate consecutive identical lines
        if clean != prev_line:
            text_lines.append(clean)
            prev_line = clean

    return "\n".join(text_lines)


def fetch_json_transcript(url):
    """Download JSON transcript and return plain text."""
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()

    # JSON transcript format varies; handle common structures
    if isinstance(data, list):
        return "\n".join(
            item.get("text", "") for item in data if item.get("text")
        )
    if isinstance(data, dict) and "sentences" in data:
        return "\n".join(
            s.get("text", "") for s in data["sentences"] if s.get("text")
        )
    return json.dumps(data, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Fetch transcript from a Loom video")
    parser.add_argument("url", help="Loom video URL (e.g. https://www.loom.com/share/abc123...)")
    parser.add_argument("--password", default=None, help="Password for private videos")
    parser.add_argument("--output", "-o", default=None, help="Write transcript to file instead of stdout")
    parser.add_argument("--format", choices=["text", "vtt"], default="text", help="Output format (default: text)")
    args = parser.parse_args()

    video_id = extract_video_id(args.url)
    print(f"Video ID: {video_id}", file=sys.stderr)

    result = fetch_transcript_urls(video_id, args.password)

    # Prefer VTT (has timestamps), fall back to JSON
    if result.get("captions_source_url"):
        vtt_text = fetch_vtt(result["captions_source_url"])
        if args.format == "vtt":
            output = vtt_text
        else:
            output = vtt_to_plain_text(vtt_text)
    elif result.get("source_url"):
        output = fetch_json_transcript(result["source_url"])
    else:
        sys.exit("Error: no transcript available.")

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Transcript saved to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
