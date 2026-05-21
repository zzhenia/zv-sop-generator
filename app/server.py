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

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
SOP_DIR = APP_DIR.parent  # actions/on-demand/sop-generator

# ── Import existing modules ──────────────────────────────────────────────────

sys.path.insert(0, str(SOP_DIR))
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
    load_env,
    md_to_storage_format,
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sop-app")

# ── App mode ─────────────────────────────────────────────────────────────────
# APP_MODE controls branding and which Confluence instance to use.
# "pi" = Pilot Institute (Railway), "zv" = Personal (Render)

APP_MODE = os.environ.get("APP_MODE", "pi")

BRAND = {
    "pi": {
        "title": "SOP Generator",
        "org": "Pilot Institute",
        "logo": "/static/PI_logo_hor-light.svg",
        "header_color": "#0747A6",
        "sop_prefix": "PI-SOP",
    },
    "zv": {
        "title": "SOP Generator",
        "org": "ZV",
        "logo": "",
        "header_color": "#253858",
        "sop_prefix": "ZV-SOP",
    },
}

# ── Credentials ──────────────────────────────────────────────────────────────
# Try config/keys.env first (local dev), fall back to OS environment (deployed)

try:
    _env = load_env()
except SystemExit:
    log.info("No keys.env found, reading credentials from environment variables.")
    _env = {}
    for key in os.environ:
        _env[key] = os.environ[key]

ACTIVE_INSTANCE = INSTANCES[APP_MODE]
ACTIVE_AUTH = get_auth(_env, ACTIVE_INSTANCE)
ANTHROPIC_KEY = _env.get("ANTHROPIC_API_KEY", "")

# ── Load template + system prompt at startup ─────────────────────────────────

TEMPLATE_MD = (SOP_DIR / "sop_template.md").read_text()

# Extract the Role and Writing style sections for the system prompt
SYSTEM_PROMPT = """You are a thoughtful and methodical SOP Assistant who combines strategic clarity with user-focused design thinking. You approach every SOP request with professionalism and curiosity. You communicate with honesty and precision.

Writing style: Clear, structured, and practical. Apply principles from On Writing Well (Zinsser), Stein on Writing, and On Writing (King). Emphasise clarity, concision, rhythm, and a natural yet precise tone. Avoid jargon, filler, or decorative language. No icons or emojis. Preserve the user's intent and voice.

You must generate a complete SOP in Markdown format following the template structure provided. Return ONLY the Markdown content inside a fenced code block. Do not include any commentary outside the code block."""

# ── Parent folders per mode ──────────────────────────────────────────────────

PARENT_FOLDERS = {
    "pi": {
        "SOP Catalogue (root)": "187269135",
        "Training Team": "124223489",
        "Organic Content": "30015499",
        "Course Content": "118358029",
    },
    "zv": {
        "SOPs (root)": "1397424130",
    },
}

# ── User cache ──────────────────────────────────────────────────────────────

_user_cache = {"users": [], "id_map": {}, "expires": 0}


def _fetch_confluence_users():
    """Fetch all users from the active Confluence instance, cached for 30 min."""
    now = time.time()
    if _user_cache["users"] and now < _user_cache["expires"]:
        return _user_cache["users"], _user_cache["id_map"]

    base = ACTIVE_INSTANCE["base"]
    users = []
    id_map = {}  # lowercase display name -> account_id

    # Try the confluence-users group first, fall back to site-admins
    for group in ["confluence-users", "site-admins"]:
        start = 0
        while True:
            try:
                resp = requests.get(
                    f"{base}/rest/api/group/{group}/member",
                    auth=ACTIVE_AUTH,
                    params={"limit": 200, "start": start},
                )
                if not resp.ok:
                    break
                data = resp.json()
                results = data.get("results", [])
                for u in results:
                    account_id = u.get("accountId", "")
                    display = u.get("displayName", "")
                    if not display or not account_id:
                        continue
                    # Skip app/bot accounts
                    if u.get("accountType") == "app":
                        continue
                    if display not in [x["display"] for x in users]:
                        users.append({
                            "display": display,
                            "account_id": account_id,
                        })
                        id_map[display.lower()] = account_id
                size = data.get("size", len(results))
                if len(results) < 200:
                    break
                start += size
            except Exception as e:
                log.warning("Failed to fetch group %s: %s", group, e)
                break
        if users:
            break

    # Merge in KNOWN_USERS as fallback (in case the API missed anyone)
    known_display = {
        "zhenia": "Zhenia Vasiliev",
        "roberto": "Roberto Castillejo",
        "jesse": "Jesse Ekkerd",
        "greg": "Greg Reverdiau",
        "ben": "Ben Pitroff",
    }
    for key, display in known_display.items():
        if display.lower() not in id_map and key in KNOWN_USERS:
            users.append({"display": display, "account_id": KNOWN_USERS[key]})
            id_map[display.lower()] = KNOWN_USERS[key]

    # Sort alphabetically by display name
    users.sort(key=lambda u: u["display"].lower())

    _user_cache["users"] = users
    _user_cache["id_map"] = id_map
    _user_cache["expires"] = now + 1800  # 30 min cache
    log.info("Loaded %d Confluence users for %s", len(users), APP_MODE)
    return users, id_map


# ── Next-ID cache ───────────────────────────────────────────────────────────

_next_id_cache = {"value": None, "expires": 0}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _query_next_sop_id():
    """Query Confluence SOP Catalogue for the next available ID."""
    now = time.time()
    if _next_id_cache["value"] and now < _next_id_cache["expires"]:
        return _next_id_cache["value"]

    base = ACTIVE_INSTANCE["base"]
    ids = []

    # Search both current and draft pages with the sop-metadata label
    for status in ["current", "draft"]:
        start = 0
        while True:
            cql = f'label = "sop-metadata" and status = "{status}"'
            resp = requests.get(
                f"{base}/rest/api/content/search",
                auth=ACTIVE_AUTH,
                params={"cql": cql, "limit": 50, "start": start},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                break
            for r in results:
                pr = requests.get(
                    f"{base}/api/v2/pages/{r['id']}",
                    auth=ACTIVE_AUTH,
                    params={"body-format": "storage"},
                )
                if pr.ok:
                    body = pr.json().get("body", {}).get("storage", {}).get("value", "")
                    prefix = BRAND.get(APP_MODE, BRAND["pi"])["sop_prefix"]
                    m = re.search(rf"{prefix}-(\d+)", body)
                    if m:
                        ids.append(int(m.group(1)))
            if len(results) < 50:
                break
            start += 50

    prefix = BRAND.get(APP_MODE, BRAND["pi"])["sop_prefix"]
    next_num = max(ids) + 1 if ids else 1
    result = f"{prefix}-{next_num:03d}"
    _next_id_cache["value"] = result
    _next_id_cache["expires"] = now + 300  # 5 min cache
    return result


def _invalidate_next_id_cache():
    """Clear the next-ID cache so the next query fetches fresh data."""
    _next_id_cache["value"] = None
    _next_id_cache["expires"] = 0


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
        # Check cached Confluence users first
        cached_users = _user_cache.get("users", [])
        for u in cached_users:
            if u["account_id"] == account_id:
                return f'<span class="mention">@{u["display"]}</span>'
        # Fall back to KNOWN_USERS
        known_display = {
            "zhenia": "Zhenia Vasiliev", "roberto": "Roberto Castillejo",
            "jesse": "Jesse Ekkerd", "greg": "Greg Reverdiau", "ben": "Ben Pitroff",
        }
        for name, aid in KNOWN_USERS.items():
            if aid == account_id:
                return f'<span class="mention">@{known_display.get(name, name)}</span>'
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
    users, _ = _fetch_confluence_users()
    return {
        "mode": APP_MODE,
        "brand": BRAND.get(APP_MODE, BRAND["pi"]),
        "users": users,
        "statuses": list(STATUS_COLOURS.keys()),
        "parent_folders": PARENT_FOLDERS.get(APP_MODE, {}),
    }


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

    users, _ = _fetch_confluence_users()
    user_names = ", ".join(u["display"] for u in users)
    prompt = f"""Extract the following SOP metadata from the text below. Return ONLY a JSON object with these fields:
- "title": a concise descriptive title for this SOP (do not include "[SOP]" prefix)
- "author": the person writing or presenting this process (use one of: {user_names}, or leave empty)
- "approver": the person who would approve this (use one of: {user_names}, or leave empty)
- "owner": the person or team responsible for maintaining this process (use one of: {user_names}, or leave empty)
- "tools_required": comma-separated list of tools/software mentioned (e.g. "Asana, Slack, Google Sheets")

If a field cannot be determined, use an empty string. Return the full display name (e.g. "Jesse Ekkerd"), not a handle.

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
        "author": author,
        "approver": approver,
        "owner": owner,
        "status": status,
        "review_date": review_date,
        "tools_required": tools,
        "loom_explainers": "",
    }

    user_prompt = f"""Generate a complete SOP using the following template structure:

{TEMPLATE_MD}

Metadata for the Header / Control Block:
- SOP ID: {sop_id}
- Title: {title}
- Version: v1.0
- Author: {author}
- Approver: {approver}
- Owner: {owner}
- Status: {status}
- Review Date: {review_date}
- Tools Required: {tools}

Raw input material (transcripts, notes, and process descriptions):

{raw_text}

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
    _, id_map = _fetch_confluence_users()
    storage_html = md_to_storage_format(markdown, metadata, id_map)
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
        _, id_map = _fetch_confluence_users()
        storage_html = md_to_storage_format(markdown, metadata, id_map)
        space_id = find_space_id(ACTIVE_AUTH, ACTIVE_INSTANCE["base"], ACTIVE_INSTANCE["space_key"])
        page_id, page_url = create_page(
            ACTIVE_AUTH, ACTIVE_INSTANCE["base"], space_id, title, storage_html, parent_id,
            draft=publish_as_draft,
        )
        add_label(ACTIVE_AUTH, ACTIVE_INSTANCE["base"], page_id, "sop-metadata")
        _invalidate_next_id_cache()
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
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
