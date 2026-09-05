"""
Builds docs/architecture.pdf from docs/architecture.md using WeasyPrint.
Applies typography and page geometry to ensure page count <= 3 and pristine ASCII rendering.
"""

import os
import markdown
from weasyprint import HTML

MD_PATH = "docs/architecture.md"
PDF_PATH = "docs/architecture.pdf"

CSS_STYLES = """
@page {
    size: letter portrait;
    margin: 0.35in 0.42in 0.35in 0.42in;
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 7pt;
        color: #718096;
    }
    @bottom-left {
        content: "FrontierAtlas / GraphOne AI Intelligence Pipeline Architecture Blueprint";
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        font-size: 7pt;
        color: #718096;
    }
}

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 7.6pt;
    line-height: 1.25;
    color: #1a202c;
    margin: 0;
    padding: 0;
}

h1 {
    font-size: 11.5pt;
    font-weight: 700;
    color: #1a365d;
    margin: 0 0 3px 0;
    padding-bottom: 2px;
    border-bottom: 1.2px solid #2b6cb0;
}

h2 {
    font-size: 9.0pt;
    font-weight: 700;
    color: #2b6cb0;
    margin: 6px 0 2px 0;
    padding-bottom: 1px;
    border-bottom: 1px solid #e2e8f0;
    page-break-after: avoid;
}

h3 {
    font-size: 8.0pt;
    font-weight: 600;
    color: #2d3748;
    margin: 4px 0 2px 0;
    page-break-after: avoid;
}

p {
    margin: 0 0 3px 0;
}

strong {
    color: #0f172a;
}

code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Courier, monospace;
    font-size: 7.0pt;
    background-color: #f1f5f9;
    padding: 1px 2px;
    border-radius: 2px;
    color: #0f172a;
}

pre {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Courier, monospace;
    font-size: 5.8pt;
    line-height: 1.10;
    background-color: #f8fafc;
    border: 1px solid #cbd5e1;
    border-radius: 3px;
    padding: 4px 6px;
    margin: 3px 0 4px 0;
    white-space: pre;
    overflow-x: hidden;
    page-break-inside: avoid;
}

table {
    width: 100%;
    border-collapse: collapse;
    font-size: 6.8pt;
    line-height: 1.18;
    margin: 3px 0 4px 0;
    page-break-inside: avoid;
}

th {
    background-color: #1e3a8a;
    color: #ffffff;
    font-weight: 600;
    text-align: left;
    padding: 2px 4px;
    border: 1px solid #1e3a8a;
}

td {
    padding: 2px 4px;
    border: 1px solid #e2e8f0;
    vertical-align: top;
}

tr:nth-child(even) {
    background-color: #f8fafc;
}

hr {
    border: 0;
    border-top: 1px solid #e2e8f0;
    margin: 4px 0;
}

ul, ol {
    margin: 1px 0 3px 14px;
    padding: 0;
}

li {
    margin-bottom: 1px;
}
"""

def build_pdf():
    with open(MD_PATH, "r", encoding="utf-8") as f:
        md_text = f.read()

    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"]
    )

    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>FrontierAtlas AI Intelligence Pipeline: Architectural Blueprint</title>
<style>{CSS_STYLES}</style>
</head>
<body>
{html_body}
</body>
</html>
"""

    os.makedirs(os.path.dirname(PDF_PATH), exist_ok=True)
    html_doc = HTML(string=full_html)
    rendered = html_doc.render()
    page_count = len(rendered.pages)
    rendered.write_pdf(PDF_PATH)
    print(f"✅ Generated {PDF_PATH} ({page_count} pages)")
    return page_count

if __name__ == "__main__":
    build_pdf()
