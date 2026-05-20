#!/usr/bin/env python3
"""SOP Generator web app — FastAPI backend."""

import json
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import anthropic
import requests
from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ── Paths ────────────────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"

# ── Import existing modules ──────────────────────────────────────────────────

sys.path.insert(0, str(ROOT_DIR))
from fetch_loom_transcript import (
    extract_video_id,
    fetch_json_transcript,
    fetch_transcript_urls,
    fetch_vtt,
    vtt_to_plain_text,
)
from publish_sop_to_confluence import (
    INSTANCES,
    KNOWN_USERS,
    STATUS_COLOURS,
    add_label,
    create_page,
    find_space_id,
    get_auth,
    md_to_storage_format,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sop-app")

# ── Credentials ──────────────────────────────────────────────────────────────
# Read from environment variables (set in Railway dashboard or .env locally)

_env = {
    "ZV_JIRA_EMAIL": os.environ.get("ZV_JIRA_EMAIL", ""),
    "ZV_JIRA_API_TOKEN": os.environ.get("ZV_JIRA_API_TOKEN", ""),
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
}

ZV = INSTANCES["zv"]
ZV_AUTH = get_auth(_env, ZV)
ANTHROPIC_KEY = _env.get("ANTHROPIC_API_KEY", "")

# ── Load template + system prompt at startup ─────────────────────────────────

TEMPLATE_MD = (ROOT_DIR / "sop_template.md").read_text()

# Extract the Role and Writing style sections for the system prompt
SYSTEM_PROMPT = """You are a thoughtful and methodical SOP Assistant who combines strategic clarity with user-focused design thinking. You approach every SOP request with professionalism and curiosity. You communicate with honesty and precision.

Writing style: Clear, structured, and practical. Apply principles from On Writing Well (Zinsser), Stein on Writing, and On Writing (King). Emphasise clarity, concision, rhythm, and a natural yet precise tone. Avoid jargon, filler, or decorative language. No icons or emojis. Preserve the user's intent and voice.

You must generate a complete SOP in Markdown format following the template structure provided. Return ONLY the Markdown content inside a fenced code block. Do not include any commentary outside the code block."""

# ── Known ZV parent folders ──────────────────────────────────────────────────

ZV_PARENT_FOLDERS = {
    "SOP (root)": "1201668108",
}

# ── Reverse user lookup ─────────────────────────────────────────────────────

USER_DISPLAY = {
    "zhenia": "Zhenia Vasiliev",
}

# ── Next-ID cache ───────────────────────────────────────────────────────────

_next_id_cache = {"value": None, "expires": 0}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _query_next_sop_id():
    """Query Confluence SOP Catalogue for the next available ID."""
    now = time.time()
    if _next_id_cache["value"] and now < _next_id_cache["expires"]:
        return _next_id_cache["value"]

    base = ZV["base"]
    resp = requests.get(
        f"{base}/rest/api/content/search",
        auth=ZV_AUTH,
        params={"cql": 'label = "sop-metadata"', "limit": 50},
    )
    resp.raise_for_status()
    ids = []
    for r in resp.json().get("results", []):
        pr = requests.get(
            f"{base}/api/v2/pages/{r['id']}",
            auth=ZV_AUTH,
            params={"body-format": "storage"},
        )
        if pr.ok:
            body = pr.json().get("body", {}).get("storage", {}).get("value", "")
            m = re.search(r"ZV-SOP-(\d+)", body)
            if m:
                ids.append(int(m.group(1)))

    next_num = max(ids) + 1 if ids else 1
    result = f"ZV-SOP-{next_num:03d}"
    _next_id_cache["value"] = result
    _next_id_cache["expires"] = now + 300  # 5 min cache
    return result


def confluence_html_to_preview(storage_html):
    """Convert Confluence Storage Format XHTML to browser-renderable HTML.

    Replaces Confluence-specific macros with standard HTML elements.
    """
    html = storage_html

    # Status lozenges
    def replace_status(m):
        inner = m.group(1)
        title_m = re.search(r'ac:name="title">([^<]+)', inner)
        colour_m = re.search(r'ac:name="colour">([^<]+)', inner)
        title = title_m.group(1) if title_m else "STATUS"
        colour = (colour_m.group(1) if colour_m else "Grey").lower()
        return f'<span class="status-lozenge status-lozenge--{colour}">{title}</span>'

    html = re.sub(
        r'<ac:structured-macro ac:name="status"[^>]*>(.*?)</ac:structured-macro>',
        replace_status,
        html,
        flags=re.DOTALL,
    )

    # User mentions
    def replace_user(m):
        account_id = m.group(1)
        # Reverse lookup
        for name, aid in KNOWN_USERS.items():
            if aid == account_id:
                display = USER_DISPLAY.get(name, f"@{name}")
                return f'<span class="mention">@{display}</span>'
        return f'<span class="mention">@user</span>'

    html = re.sub(
        r'<ac:link><ri:user ri:account-id="([^"]+)"[^/]*/></ac:link>',
        replace_user,
        html,
    )

    # Date macros
    html = re.sub(
        r'<time datetime="(\d{4}-\d{2}-\d{2})"[^/]*/>',
        r'<span class="date-badge">\1</span>',
        html,
    )

    # Page Properties macro (details) — unwrap to show the table directly
    html = re.sub(
        r'<ac:structured-macro ac:name="details"[^>]*>\s*<ac:rich-text-body>(.*?)</ac:rich-text-body>\s*</ac:structured-macro>',
        r'<div class="page-properties">\1</div>',
        html,
        flags=re.DOTALL,
    )

    # Code blocks
    def replace_code(m):
        inner = m.group(1)
        lang_m = re.search(r'ac:name="language">([^<]+)', inner)
        code_m = re.search(r'<!\[CDATA\[(.*?)\]\]>', inner, re.DOTALL)
        lang = lang_m.group(1) if lang_m else ""
        code = code_m.group(1) if code_m else ""
        return f'<pre><code class="language-{lang}">{code}</code></pre>'

    html = re.sub(
        r'<ac:structured-macro ac:name="code"[^>]*>(.*?)</ac:structured-macro>',
        replace_code,
        html,
        flags=re.DOTALL,
    )

    # Remove any remaining local-id attributes
    html = re.sub(r'\s*(?:local-id|ac:local-id|ac:macro-id)="[^"]*"', "", html)

    return html


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="SOP Generator")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config():
    return {
        "users": [
            {"key": k, "display": USER_DISPLAY.get(k, k)} for k in KNOWN_USERS
        ],
        "statuses": list(STATUS_COLOURS.keys()),
        "parent_folders": ZV_PARENT_FOLDERS,
    }


SOPS_ROOT_ID = "1201668108"  # "SOP" page in ZV Confluence

_folder_cache = {"tree": None, "expires": 0}


def _fetch_folder_tree():
    """Fetch the full folder tree under the SOPs root from Confluence."""
    now = time.time()
    if _folder_cache["tree"] and now < _folder_cache["expires"]:
        return _folder_cache["tree"]

    base = ZV["base"]

    def get_child_folders(page_id, depth=0):
        """Recursively get child pages using the v1 child/page endpoint."""
        resp = requests.get(
            f"{base}/rest/api/content/{page_id}/child/page",
            auth=ZV_AUTH,
            params={"limit": 50},
        )
        if not resp.ok:
            return []
        children = sorted(resp.json().get("results", []), key=lambda x: x["title"])
        results = []
        for page in children:
            indent = "\u2003" * depth  # em-space for visual indent
            prefix = "└ " if depth > 0 else ""
            results.append({
                "id": page["id"],
                "title": page["title"],
                "label": f"{indent}{prefix}{page['title']}",
                "depth": depth,
            })
            if depth < 4:
                results.extend(get_child_folders(page["id"], depth + 1))
        return results

    tree = [{
        "id": SOPS_ROOT_ID,
        "title": "SOPs (root)",
        "label": "SOPs (root)",
        "depth": 0,
    }]
    tree.extend(get_child_folders(SOPS_ROOT_ID, 1))

    _folder_cache["tree"] = tree
    _folder_cache["expires"] = now + 600  # 10 min cache
    return tree


@app.get("/api/folders")
def get_folders():
    try:
        return _fetch_folder_tree()
    except Exception as e:
        log.error("Failed to fetch folder tree: %s", e)
        return [{"id": v, "title": k, "label": k, "depth": 0}
                for k, v in ZV_PARENT_FOLDERS.items()]


@app.get("/api/next-id")
def get_next_id():
    try:
        next_id = _query_next_sop_id()
        return {"next_id": next_id}
    except Exception as e:
        log.error("Failed to query next SOP ID: %s", e)
        raise HTTPException(500, f"Failed to query SOP catalogue: {e}")


@app.post("/api/fetch-loom")
def fetch_loom(body: dict):
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "No URL provided.")
    try:
        video_id = extract_video_id(url)
    except SystemExit as e:
        raise HTTPException(400, str(e))

    try:
        result = fetch_transcript_urls(video_id)
    except SystemExit as e:
        raise HTTPException(400, str(e))

    try:
        if result.get("captions_source_url"):
            vtt = fetch_vtt(result["captions_source_url"])
            transcript = vtt_to_plain_text(vtt)
        elif result.get("source_url"):
            transcript = fetch_json_transcript(result["source_url"])
        else:
            raise HTTPException(404, "No transcript available for this video.")
    except SystemExit as e:
        raise HTTPException(400, str(e))

    return {"transcript": transcript, "video_id": video_id}


@app.post("/api/upload")
async def upload_file(file: UploadFile):
    content = await file.read()
    name = file.filename or ""

    if name.endswith(".pdf"):
        try:
            import pdfplumber

            import io

            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n\n".join(
                    page.extract_text() or "" for page in pdf.pages
                )
        except ImportError:
            raise HTTPException(
                501, "PDF extraction requires pdfplumber. Install it with: pip install pdfplumber"
            )
    else:
        text = content.decode("utf-8", errors="replace")

    return {"text": text, "filename": name}


@app.post("/api/extract-metadata")
def extract_metadata(body: dict):
    """Use Claude to extract SOP metadata fields from raw input text."""
    raw_text = body.get("raw_text", "").strip()
    if not raw_text:
        raise HTTPException(400, "No input text provided.")
    if not ANTHROPIC_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured.")

    known_names = ", ".join(f"@{k}" for k in KNOWN_USERS)
    prompt = f"""Extract the following SOP metadata from the text below. Return ONLY a JSON object with these fields:
- "title": a concise descriptive title for this SOP (do not include "[SOP]" prefix)
- "author": the person writing or presenting this process (use one of: {known_names}, or leave empty)
- "approver": the person who would approve this (use one of: {known_names}, or leave empty)
- "owner": the person or team responsible for maintaining this process (use one of: {known_names}, or leave empty)
- "tools_required": comma-separated list of tools/software mentioned (e.g. "Asana, Slack, Google Sheets")

If a field cannot be determined, use an empty string. For author/approver/owner, return just the @handle (e.g. "@zhenia"), not the full name.

Text:
{raw_text[:4000]}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        # Extract JSON from response
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(raw)
    except Exception as e:
        log.error("Metadata extraction error: %s", e)
        raise HTTPException(500, f"Extraction failed: {e}")


@app.post("/api/generate")
def generate_sop(body: dict):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, "Title is required.")

    # Auto-prefix with [SOP]
    if not title.startswith("[SOP]"):
        title = f"[SOP] {title}"

    sop_id = body.get("sop_id", "")
    author = body.get("author", "")
    approver = body.get("approver", "")
    owner = body.get("owner", "")
    tools = body.get("tools_required", "")
    status = body.get("status", "DRAFT")
    loom_urls = body.get("loom_urls", [])
    raw_text = body.get("raw_text", "")
    review_date = body.get("review_date", "")

    if not review_date:
        # Default to 6 months from now
        today = date.today()
        review_date = today.replace(
            year=today.year + (1 if today.month > 6 else 0),
            month=(today.month + 6 - 1) % 12 + 1,
        ).isoformat()

    metadata = {
        "sop_id": sop_id,
        "version": "v1.0",
        "author": f"@{author}",
        "approver": f"@{approver}",
        "owner": f"@{owner}",
        "status": status,
        "review_date": review_date,
        "tools_required": tools,
        "loom_explainers": ", ".join(loom_urls),
    }

    user_prompt = f"""Generate a complete SOP using the following template structure:

{TEMPLATE_MD}

Metadata for the Header / Control Block:
- SOP ID: {sop_id}
- Title: {title}
- Version: v1.0
- Author: @{author}
- Approver: @{approver}
- Owner: @{owner}
- Status: {status}
- Review Date: {review_date}
- Tools Required: {tools}

Raw input material (transcripts, notes, and process descriptions):

{raw_text}

IMPORTANT:
- Today's date is {date.today().isoformat()}. Use this as the date in the Version History table.
- Do NOT include an H1 title at the top -- the page title is set separately.
- Start directly with ## Header / Control Block.

Generate a complete, well-structured SOP based on this material. Follow the template exactly. Include all sections: Purpose, Scope, Roles and Responsibilities, Review and Approval, Procedure (with numbered steps), Checklist (with checkbox items), and Review and Maintenance with a Version History table. Return ONLY the Markdown inside a fenced code block."""

    if not ANTHROPIC_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured.")

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_response = response.content[0].text
    except Exception as e:
        log.error("Claude API error: %s", e)
        raise HTTPException(500, f"Claude API error: {e}")

    # Extract markdown from fenced code block if present
    md_match = re.search(r"```(?:markdown)?\s*\n(.*?)```", raw_response, re.DOTALL)
    markdown = md_match.group(1).strip() if md_match else raw_response.strip()

    # Generate HTML preview
    storage_html = md_to_storage_format(markdown, metadata)
    preview_html = confluence_html_to_preview(storage_html)

    return {
        "title": title,
        "markdown": markdown,
        "html_preview": preview_html,
        "metadata": metadata,
    }


@app.post("/api/publish")
def publish_sop(body: dict):
    title = body.get("title", "")
    markdown = body.get("markdown", "")
    metadata = body.get("metadata", {})
    parent_id = body.get("parent_id", "")

    publish_as_draft = body.get("publish_as_draft", False)

    if not title or not markdown or not parent_id:
        raise HTTPException(400, "Title, markdown, and parent_id are required.")

    try:
        storage_html = md_to_storage_format(markdown, metadata)
        space_id = find_space_id(ZV_AUTH, ZV["base"], ZV["space_key"])
        page_id, page_url = create_page(
            ZV_AUTH, ZV["base"], space_id, title, storage_html, parent_id,
            draft=publish_as_draft,
        )
        add_label(ZV_AUTH, ZV["base"], page_id, "sop-metadata")
    except SystemExit as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        log.error("Publish error: %s", e)
        raise HTTPException(500, f"Publish failed: {e}")

    return {"page_id": page_id, "page_url": page_url}


@app.post("/api/download/md")
def download_md(body: dict):
    markdown = body.get("markdown", "")
    filename = body.get("filename", "sop.md")
    return Response(
        content=markdown.encode("utf-8"),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/download/pdf")
def download_pdf(body: dict):
    html = body.get("html", "")
    filename = body.get("filename", "sop.pdf")

    # Wrap in a full HTML document for weasyprint
    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; line-height: 1.6; color: #172b4d; max-width: 800px; margin: 0 auto; padding: 40px; }}
h1 {{ font-size: 24px; }} h2 {{ font-size: 20px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }} h3 {{ font-size: 16px; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }} th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }} th {{ background: #f4f5f7; font-weight: 600; }}
ul, ol {{ padding-left: 24px; }} li {{ margin-bottom: 4px; }}
.status-lozenge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 700; text-transform: uppercase; }}
.status-lozenge--blue {{ background: #deebff; color: #0747a6; }} .status-lozenge--green {{ background: #e3fcef; color: #006644; }}
.status-lozenge--yellow {{ background: #fff0b3; color: #172b4d; }} .status-lozenge--red {{ background: #ffebe6; color: #bf2600; }}
.status-lozenge--grey {{ background: #dfe1e6; color: #42526e; }}
.mention {{ color: #0052cc; font-weight: 500; }} .date-badge {{ color: #0052cc; }}
.page-properties {{ background: #f4f5f7; padding: 16px; border-radius: 4px; margin: 16px 0; }}
pre {{ background: #f4f5f7; padding: 12px; border-radius: 4px; overflow-x: auto; }} code {{ font-size: 12px; }}
</style>
</head><body>{html}</body></html>"""

    try:
        from weasyprint import HTML

        pdf_bytes = HTML(string=full_html).write_pdf()
    except ImportError:
        raise HTTPException(
            501, "PDF export requires weasyprint. Install: pip install weasyprint"
        )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Static files mount (must be last) ───────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="root")

# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8002))
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
