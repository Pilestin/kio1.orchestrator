// Markdown as models write it, turned into safe markup.
//
// Small on purpose: the console loads nothing from anywhere else, and what a
// model writes in an answer is a known, modest subset — paragraphs, lists,
// tables, code, emphasis, links. Everything is escaped first; only those
// marks become markup, and a link is only ever http(s). No DOM here, so it
// can be tested on its own.

/** Escape text for HTML, attributes included. */
export function escapeHtml(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const FENCE = /^\s*(```|~~~)\s*([\w+-]*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

/** Inline marks inside one stretch of text that is not code. */
function inlineText(text, options) {
  let html = escapeHtml(text);

  // Links first, so the brackets of [text](url) are not read as citations.
  html = html.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, function (match, label, url) {
    return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
  });

  html = html
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    // A single * or _ only at a word's edge, so snake_case stays as it is.
    .replace(/(^|[\s(])\*([^*\s][^*\n]*?)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_([^_\s][^_\n]*?)_(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>");

  return options.citations ? options.citations(html) : html;
}

/** Inline markup for a line: code spans are set aside before anything else. */
export function renderInline(text, options) {
  const opts = options || {};

  return String(text).split(/(`[^`\n]+`)/).map(function (part, index) {
    return index % 2 === 1
      ? "<code>" + escapeHtml(part.slice(1, -1)) + "</code>"
      : inlineText(part, opts);
  }).join("");
}

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (cell) {
    return cell.trim();
  });
}

function tableHtml(lines, options) {
  const head = cells(lines[0]);
  const rows = lines.slice(2).map(cells);

  return '<div class="md-table"><table><thead><tr>'
    + head.map(function (cell) { return "<th>" + renderInline(cell, options) + "</th>"; }).join("")
    + "</tr></thead><tbody>"
    + rows.map(function (row) {
        return "<tr>" + head.map(function (_, index) {
          return "<td>" + renderInline(row[index] || "", options) + "</td>";
        }).join("") + "</tr>";
      }).join("")
    + "</tbody></table></div>";
}

/** A run of list items, nested by indentation. */
function listHtml(items, options) {
  const ordered = /\d/.test(items[0].marker);
  const base = items[0].indent;
  let html = ordered ? "<ol>" : "<ul>";
  let index = 0;

  while (index < items.length) {
    const item = items[index];
    const children = [];
    index += 1;

    while (index < items.length && items[index].indent > base) {
      children.push(items[index]);
      index += 1;
    }

    html += "<li>" + renderInline(item.text, options)
      + (children.length ? listHtml(children, options) : "") + "</li>";
  }

  return html + (ordered ? "</ol>" : "</ul>");
}

/** Markdown to markup.
 *
 * @param {string} text What the model wrote.
 * @param {{citations?: (html: string) => string}} [options] `citations` gets
 *   each stretch of escaped prose outside code, to mark things such as
 *   `[Python]` — the answer's sources.
 * @returns {string} Safe HTML.
 */
export function renderMarkdown(text, options) {
  const opts = options || {};
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let paragraph = [];
  let index = 0;

  function flush() {
    if (paragraph.length) {
      out.push("<p>" + paragraph.map(function (line) { return renderInline(line, opts); }).join("<br>") + "</p>");
      paragraph = [];
    }
  }

  while (index < lines.length) {
    const line = lines[index];
    let match;

    if (!line.trim()) {
      flush();
      index += 1;
    } else if ((match = FENCE.exec(line))) {
      flush();
      const code = [];
      index += 1;

      while (index < lines.length && !new RegExp("^\\s*" + match[1] + "\\s*$").test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }

      index += 1;  // the closing fence, or the end
      out.push('<pre class="md-code"><code' + (match[2] ? ' data-lang="' + escapeHtml(match[2]) + '"' : "")
        + ">" + escapeHtml(code.join("\n")) + "</code></pre>");
    } else if ((match = HEADING.exec(line))) {
      flush();
      // A model's "#" is a section of an answer, not a page title.
      const level = Math.min(match[1].length + 2, 6);
      out.push("<h" + level + ">" + renderInline(match[2], opts) + "</h" + level + ">");
      index += 1;
    } else if (RULE.test(line)) {
      flush();
      out.push("<hr>");
      index += 1;
    } else if (QUOTE.test(line)) {
      flush();
      const quoted = [];

      while (index < lines.length && (match = QUOTE.exec(lines[index]))) {
        quoted.push(match[1]);
        index += 1;
      }

      out.push("<blockquote>" + renderMarkdown(quoted.join("\n"), opts) + "</blockquote>");
    } else if (line.indexOf("|") !== -1 && index + 1 < lines.length && TABLE_SEPARATOR.test(lines[index + 1])) {
      flush();
      const rows = [line, lines[index + 1]];
      index += 2;

      while (index < lines.length && lines[index].indexOf("|") !== -1 && lines[index].trim()) {
        rows.push(lines[index]);
        index += 1;
      }

      out.push(tableHtml(rows, opts));
    } else if (ITEM.test(line)) {
      flush();
      const items = [];

      while (index < lines.length) {
        match = ITEM.exec(lines[index]);

        if (match) {
          items.push({ indent: match[1].replace(/\t/g, "  ").length, marker: match[2], text: match[3] });
        } else if (lines[index].trim() && /^\s+/.test(lines[index]) && items.length) {
          // A wrapped line belongs to the item above it.
          items[items.length - 1].text += " " + lines[index].trim();
        } else {
          break;
        }

        index += 1;
      }

      out.push(listHtml(items, opts));
    } else {
      paragraph.push(line);
      index += 1;
    }
  }

  flush();

  return out.join("");
}
