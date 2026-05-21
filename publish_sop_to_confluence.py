#!/usr/bin/env python3
"""Publish an SOP to Confluence (Pilot Institute or ZV) with Page Properties macro."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

# ── Confluence instances ─────────────────────────────────────────────────────

INSTANCES = {
    "pi": {
        "label": "Pilot Institute",
        "base": "https://pilotinstitute.atlassian.net/wiki",
        "space_key": "~712020019e327f02364db3ba19c0b588cc1230",
        "email_key": "PI_CONFLUENCE_EMAIL",
        "token_key": "PI_CONFLUENCE_API_TOKEN",
    },
    "zv": {
        "label": "ZV",
        "base": "https://zvi.atlassian.net/wiki",
        "space_key": "~288856893",
        "email_key": "ZV_JIRA_EMAIL",
        "token_key": "ZV_JIRA_API_TOKEN",
    },
}

# ── Status badge colours ────────────────────────────────────────────────────

STATUS_COLOURS = {
    "DRAFT": "Blue",
    "IN REVIEW": "Yellow",
    "VERIFIED": "Green",
    "APPROVED": "Green",
    "REVIEW DUE": "Red",
    "ARCHIVED": "Grey",
}

# ── Known Confluence account IDs ─────────────────────────────────────────────

KNOWN_USERS = {
    "zhenia": "712020:019e327f-0236-4db3-ba19-c0b588cc1230",
    "roberto": "712020:2d84d7dc-bbae-4d09-bc09-a74ff5aa4d56",
    "jesse": "712020:034b151c-fca9-4487-b7c0-01e97b8612cc",
    "greg": "5af22c2ca741a92a65124569",
    "ben": "712020:e55b6ea2-bb2d-4ea3-82ee-32b5ea2c98c4",
}


def load_env():
    """Load credentials from config/keys.env."""
    keys_path = Path(__file__).resolve().parents[3] / "config" / "keys.env"
    if not keys_path.exists():
        sys.exit(f"Error: {keys_path} not found.")
    env = {}
    for line in keys_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def get_auth(env, instance):
    email = env.get(instance["email_key"], "")
    token = env.get(instance["token_key"], "")
    if not email or not token:
        sys.exit(
            f"Error: {instance['email_key']} and {instance['token_key']} "
            f"must be set in config/keys.env."
        )
    return (email, token)


# ── Markdown to Confluence Storage Format ────────────────────────────────────


def user_mention(account_id):
    """Generate a Confluence user mention macro."""
    return (
        f'<ac:link><ri:user ri:account-id="{account_id}" /></ac:link>'
    )


def status_badge(status_text):
    """Generate a Confluence status lozenge macro."""
    colour = STATUS_COLOURS.get(status_text.upper(), "Yellow")
    return (
        f'<ac:structured-macro ac:name="status" ac:schema-version="1">'
        f'<ac:parameter ac:name="title">{status_text.upper()}</ac:parameter>'
        f'<ac:parameter ac:name="colour">{colour}</ac:parameter>'
        f'</ac:structured-macro>'
    )


def date_macro(date_str):
    """Generate a Confluence date macro from YYYY-MM-DD."""
    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return f'<time datetime="{date_str}" />'
    return date_str


def resolve_user(name, id_map=None):
    """Resolve a user name or @mention to a Confluence account ID mention.

    Checks (in order): account_id format, live id_map from Confluence API,
    hardcoded KNOWN_USERS. Falls back to plain text if unresolved.
    """
    clean = name.strip().lstrip("@")
    if not clean:
        return ""
    # Already an account ID
    if ":" in clean and len(clean) > 20:
        return user_mention(clean)
    # Check live id_map (display name -> account_id)
    if id_map and clean.lower() in id_map:
        return user_mention(id_map[clean.lower()])
    # Check hardcoded KNOWN_USERS (short key -> account_id)
    if clean.lower() in KNOWN_USERS:
        return user_mention(KNOWN_USERS[clean.lower()])
    # Unresolved — render as plain text
    return clean


def inline_format(text):
    """Apply inline Markdown formatting: bold, italic, links, code."""
    text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.*?)\*", r"<em>\1</em>", text)
    text = re.sub(r"`(.*?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', text)
    return text


def md_table_to_html(lines):
    """Convert Markdown table lines to an HTML table."""
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    if len(rows) < 2:
        return ""
    # Skip separator row (row index 1)
    header = rows[0]
    data_rows = [r for i, r in enumerate(rows) if i > 1]

    html = '<table data-table-width="760" data-layout="default"><colgroup>'
    html += '<col style="width: 150.0px;" />'
    for _ in range(len(header) - 1):
        html += '<col style="width: 200.0px;" />'
    html += '</colgroup><tbody>'

    # Header row
    html += "<tr>"
    for cell in header:
        html += f"<th><p>{inline_format(cell)}</p></th>"
    html += "</tr>"

    for row in data_rows:
        html += "<tr>"
        for cell in row:
            html += f"<td><p>{inline_format(cell)}</p></td>"
        html += "</tr>"

    html += "</tbody></table>"
    return html


def build_control_block(metadata, id_map=None):
    """Build the Header / Control Block as a Page Properties (details) macro."""
    rows = []

    def add_row(label, value):
        rows.append(f"<tr><th><p>{label}</p></th><td><p>{value}</p></td></tr>")

    add_row("ID", metadata.get("sop_id", ""))
    add_row("Version", metadata.get("version", "v1.0"))
    add_row("Author", resolve_user(metadata.get("author", ""), id_map))
    add_row("Approver", resolve_user(metadata.get("approver", ""), id_map))
    add_row("Owner", resolve_user(metadata.get("owner", ""), id_map))
    add_row("Status", status_badge(metadata.get("status", "DRAFT")))
    add_row("Review Date", date_macro(metadata.get("review_date", "")))
    add_row("Tools Required", inline_format(metadata.get("tools_required", "")))

    loom = metadata.get("loom_explainers", "")
    if loom:
        loom_links = []
        for url in loom.split(","):
            url = url.strip()
            if url:
                loom_links.append(f'<a href="{url}" data-card-appearance="inline">{url}</a>')
        add_row("Loom Explainers", "<br />".join(loom_links) if loom_links else "")
    else:
        add_row("Loom Explainers", "")

    table = (
        '<table data-table-width="760" data-layout="default">'
        '<colgroup><col style="width: 121.0px;" />'
        '<col style="width: 525.0px;" /></colgroup>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )

    return (
        '<ac:structured-macro ac:name="details" ac:schema-version="1" '
        'data-layout="default">'
        f"<ac:rich-text-body>{table}</ac:rich-text-body>"
        "</ac:structured-macro>"
    )


def _parse_list_line(line):
    """Parse a line as a list item, returning (indent_level, kind, text) or None.

    indent_level: 0 for top-level, 1 for one indent, etc.
    kind: 'ol' for numbered, 'ul' for bullet
    """
    # Ordered: "1. text" or "   1. text"
    m = re.match(r"^(\s*)\d+\.\s+(.*)", line)
    if m:
        indent = len(m.group(1)) // 2  # 2-space or tab indents
        return (indent, "ol", m.group(2))
    # Unordered: "- text" or "  - text" or "* text"
    m = re.match(r"^(\s*)[-*]\s+(.*)", line)
    if m:
        # Skip checkbox lines — handled separately
        if re.match(r"^\[([ xX])\]\s", m.group(2)):
            return None
        indent = len(m.group(1)) // 2
        return (indent, "ul", m.group(2))
    return None


def _close_lists(stack):
    """Close all open list tags and return the closing HTML fragments."""
    parts = []
    while stack:
        parts.append(f"</{stack.pop()}>")
        # Close the parent <li> for nested lists (all but the outermost)
        if stack:
            parts.append("</li>")
    return parts


def md_to_storage_format(md_text, metadata, id_map=None):
    """Convert SOP Markdown to Confluence Storage Format (XHTML).

    The control block table is detected and replaced with the Page Properties
    macro. All other content is converted to standard XHTML.
    """
    lines = md_text.split("\n")
    html_parts = []
    i = 0
    list_stack = []  # stack of open list tags ('ol' or 'ul')
    skipped_h1 = False  # skip the first H1 (page title is shown by Confluence)

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Code blocks (triple backtick fences) ─────────────────────
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i].rstrip())
                i += 1
            i += 1  # skip closing ```
            code_content = "\n".join(code_lines)
            # Escape HTML entities in code
            code_content = code_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if lang:
                html_parts.append(
                    f'<ac:structured-macro ac:name="code" ac:schema-version="1">'
                    f'<ac:parameter ac:name="language">{lang}</ac:parameter>'
                    f'<ac:plain-text-body><![CDATA[{code_content}]]></ac:plain-text-body>'
                    f'</ac:structured-macro>'
                )
            else:
                html_parts.append(f"<pre><code>{code_content}</code></pre>")
            continue

        # ── Detect and replace the control block table ───────────────
        if stripped == "## Header / Control Block":
            html_parts.append("<h2>Header / Control Block</h2>")
            html_parts.append(build_control_block(metadata, id_map))
            # Skip until next --- or ## heading
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if s.startswith("## ") and s != "## Header / Control Block":
                    break
                if s == "---":
                    i += 1
                    break
                i += 1
            continue

        # ── Headings ─────────────────────────────────────────────────
        heading_match = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if heading_match:
            # Close any open lists before a heading
            html_parts.extend(_close_lists(list_stack))
            level = len(heading_match.group(1))
            # Skip the first H1 — Confluence already shows the page title
            if level == 1 and not skipped_h1:
                skipped_h1 = True
                i += 1
                continue
            text = inline_format(heading_match.group(2))
            html_parts.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # ── Horizontal rules ─────────────────────────────────────────
        if stripped == "---":
            html_parts.extend(_close_lists(list_stack))
            i += 1
            continue

        # ── Tables ───────────────────────────────────────────────────
        if "|" in stripped and stripped.startswith("|"):
            html_parts.extend(_close_lists(list_stack))
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            html_parts.append(md_table_to_html(table_lines))
            continue

        # ── Lists (ordered, unordered, nested) ──────────────────────
        list_item = _parse_list_line(line)
        if list_item:
            indent, kind, text = list_item
            target_depth = indent + 1  # depth 1 = top-level
            tag = "ol" if kind == "ol" else "ul"

            if len(list_stack) < target_depth:
                # Going deeper — open nested list(s)
                # Remove </li> from the previous item so the sub-list nests inside it
                if html_parts and html_parts[-1].endswith("</li>"):
                    html_parts[-1] = html_parts[-1][:-5]  # strip </li>
                while len(list_stack) < target_depth:
                    html_parts.append(f"<{tag}>")
                    list_stack.append(tag)
            elif len(list_stack) > target_depth:
                # Coming back up — close nested list(s) and their parent <li>
                while len(list_stack) > target_depth:
                    html_parts.append(f"</{list_stack.pop()}>")
                    html_parts.append("</li>")
                # Swap list type at this depth if needed
                if list_stack and list_stack[-1] != tag:
                    html_parts.append(f"</{list_stack.pop()}>")
                    html_parts.append(f"<{tag}>")
                    list_stack.append(tag)
            else:
                # Same depth — swap list type if needed
                if list_stack and list_stack[-1] != tag:
                    html_parts.append(f"</{list_stack.pop()}>")
                    html_parts.append(f"<{tag}>")
                    list_stack.append(tag)

            html_parts.append(f"<li><p>{inline_format(text)}</p></li>")
            i += 1
            continue

        # ── Checkbox items ───────────────────────────────────────────
        checkbox_match = re.match(r"^[-*]\s*\[([ xX])\]\s*(.*)", stripped)
        if checkbox_match:
            html_parts.extend(_close_lists(list_stack))
            checked = checkbox_match.group(1).lower() == "x"
            text = inline_format(checkbox_match.group(2))
            prefix = "[x]" if checked else "[ ]"
            html_parts.append(f"<p>{prefix} {text}</p>")
            i += 1
            continue

        # ── Empty lines ──────────────────────────────────────────────
        if not stripped:
            i += 1
            continue

        # ── Paragraphs ───────────────────────────────────────────────
        html_parts.extend(_close_lists(list_stack))
        html_parts.append(f"<p>{inline_format(stripped)}</p>")
        i += 1

    # Close any remaining open lists
    html_parts.extend(_close_lists(list_stack))

    return "\n".join(html_parts)


# ── Confluence API ───────────────────────────────────────────────────────────


def find_space_id(auth, base, space_key):
    """Look up the numeric space ID from a space key."""
    resp = requests.get(
        f"{base}/api/v2/spaces",
        auth=auth,
        params={"keys": space_key, "limit": 1},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        sys.exit(f"Error: space '{space_key}' not found.")
    return results[0]["id"]


def create_page(auth, base, space_id, title, body_html, parent_id, *, draft=False):
    """Create a new Confluence page, appending a counter if the title exists."""
    url = f"{base}/api/v2/pages"
    page_status = "draft" if draft else "current"

    for attempt in range(1, 10):
        try_title = title if attempt == 1 else f"{title} ({attempt})"
        payload = {
            "spaceId": space_id,
            "status": page_status,
            "title": try_title,
            "parentId": parent_id,
            "body": {
                "representation": "storage",
                "value": body_html,
            },
        }
        resp = requests.post(url, auth=auth, json=payload)
        if resp.ok:
            data = resp.json()
            page_id = data["id"]
            page_url = (
                data.get("_links", {}).get("base", base)
                + data.get("_links", {}).get("webui", f"/pages/{page_id}")
            )
            return page_id, page_url
        if resp.status_code == 400 and "already exists" in resp.text:
            continue
        print(f"Error {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()

    sys.exit(f"Error: could not create page -- title '{title}' has too many duplicates.")


def add_label(auth, base, page_id, label):
    """Add a label to a Confluence page (v1 API)."""
    # v2 doesn't support labels yet, use v1
    url = f"{base}/rest/api/content/{page_id}/label"
    payload = [{"prefix": "global", "name": label}]
    resp = requests.post(url, auth=auth, json=payload)
    if not resp.ok:
        print(f"Warning: could not add label '{label}': {resp.status_code}", file=sys.stderr)


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Publish SOP to Confluence")
    parser.add_argument("--title", required=True, help="Page title (e.g. 'SOP YouTube Scriptwriting')")
    parser.add_argument("--body-file", required=True, help="Path to SOP Markdown file")
    parser.add_argument("--metadata-file", required=True, help="Path to JSON file with SOP metadata")
    parser.add_argument(
        "--target",
        choices=["pi", "zv"],
        required=True,
        help="Confluence instance: 'pi' (Pilot Institute) or 'zv'",
    )
    parser.add_argument("--parent-id", required=True, help="Confluence parent page ID to publish under")
    parser.add_argument("--dry-run", action="store_true", help="Print HTML without publishing")
    args = parser.parse_args()

    body_path = Path(args.body_file)
    if not body_path.exists():
        sys.exit(f"Error: {body_path} not found.")

    meta_path = Path(args.metadata_file)
    if not meta_path.exists():
        sys.exit(f"Error: {meta_path} not found.")

    metadata = json.loads(meta_path.read_text())
    md_text = body_path.read_text()
    instance = INSTANCES[args.target]
    body_html = md_to_storage_format(md_text, metadata)

    if args.dry_run:
        print(f"=== {instance['label']} Confluence Storage Format ===")
        print(body_html)
        print(f"\n=== Metadata ===")
        print(json.dumps(metadata, indent=2))
        return

    env = load_env()
    auth = get_auth(env, instance)

    # Resolve space ID
    space_key = instance["space_key"]
    if not space_key:
        sys.exit("Error: space_key not configured for this instance.")
    space_id = find_space_id(auth, instance["base"], space_key)

    print(f"Publishing to {instance['label']} Confluence...")
    page_id, page_url = create_page(
        auth, instance["base"], space_id, args.title, body_html, args.parent_id
    )

    # Add sop-metadata label so the SOP Catalogue picks it up
    add_label(auth, instance["base"], page_id, "sop-metadata")

    print(f"Page created: {page_url}")
    print(f"Page ID: {page_id}")
    print(f"Label 'sop-metadata' added.")


if __name__ == "__main__":
    main()
