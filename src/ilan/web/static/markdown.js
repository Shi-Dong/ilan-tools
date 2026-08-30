/* A small Markdown renderer for agent output.
 *
 * Agent replies are Markdown, and showing them as raw text means reading
 * literal ``**bold**`` and unformatted code fences on a phone. This renders the
 * subset agents actually emit: fenced and inline code, headings, bold, italic,
 * strikethrough, links, bare URLs, ordered/unordered/task lists (nested),
 * blockquotes, tables, and horizontal rules.
 *
 * SECURITY. A message is untrusted input — an agent can quote anything,
 * including a user's file that contains a <script> tag. Two rules keep that
 * safe, and both must hold for every path through this file:
 *
 *   1. Every piece of message text is escaped before it becomes HTML. Tags in
 *      the output are only ever ones this file writes; nothing in the input can
 *      become one.
 *   2. Link targets are scheme-checked. Escaping alone would happily preserve
 *      ``javascript:...``, which is a script the moment it is clicked.
 *
 * Anything this renderer does not recognise is emitted as escaped text, so
 * unsupported syntax degrades to exactly what the old plain-text view showed.
 * Nothing is ever dropped.
 */
'use strict';

const MD = (() => {
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  /** Return *raw* if it is a safe link target, else null.
   *
   * Relative links are allowed; an explicit scheme must be http, https or
   * mailto. Everything else (``javascript:``, ``data:``, ``vbscript:``…) is
   * refused, and the caller renders the link as plain text instead.
   */
  function safeUrl(raw) {
    const url = String(raw).trim();
    if (/^(https?:|mailto:)/i.test(url)) return url;
    // A leading scheme this function did not just allow is rejected outright.
    // The character class matches RFC 3986 scheme syntax.
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(url)) return null;
    return url;
  }

  // Inline code is pulled out before any other inline rule runs, so ``*`` or
  // ``_`` inside a code span is never mistaken for emphasis.
  const PLACEHOLDER = '\u0000';

  function inline(text) {
    const codes = [];
    // Double-backtick form first: it may legitimately contain a single tick.
    let out = String(text)
      .replaceAll(PLACEHOLDER, '')
      .replace(/``([^`]+)``/g, (_m, code) => {
        codes.push(code);
        return `${PLACEHOLDER}${codes.length - 1}${PLACEHOLDER}`;
      })
      .replace(/`([^`\n]+)`/g, (_m, code) => {
        codes.push(code);
        return `${PLACEHOLDER}${codes.length - 1}${PLACEHOLDER}`;
      });

    out = escapeHtml(out);

    // [text](url) — the target is scheme-checked; a refused one degrades to
    // the literal markdown rather than silently vanishing. An optional title
    // after the URL is matched loosely and dropped: it cannot contain ')', so
    // the match still ends at the right place, and matching it loosely means a
    // title containing quotes does not defeat the whole link.
    out = out.replace(/\[([^\]\n]*)\]\(([^)\s]+)(?:\s+[^)\n]*)?\)/g,
      (whole, label, target) => {
        const href = safeUrl(target.replaceAll('&amp;', '&'));
        if (href === null) return whole;
        return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      });

    // Bare URLs, but not ones already inside an href="..." from the step above.
    out = out.replace(/(^|[\s(])(https?:\/\/[^\s<>()]+)/g,
      (_m, lead, url) =>
        `${lead}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);

    out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    // ``__bold__`` is deliberately NOT supported. It is the rarer of the two
    // bold spellings, and in agent output about Python it collides constantly
    // with dunder names: rendering ``__init__`` as a bold "init" silently eats
    // the underscores that carry the meaning. ``**bold**`` covers the case.
    // Emphasis only at a word boundary, so snake_case identifiers and glob
    // patterns in agent output survive intact.
    out = out.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
    out = out.replace(/(^|[^\w_])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');

    return out.replace(
      new RegExp(`${PLACEHOLDER}(\\d+)${PLACEHOLDER}`, 'g'),
      (_m, i) => `<code>${escapeHtml(codes[Number(i)])}</code>`,
    );
  }

  const RE_FENCE = /^\s*(?:```|~~~)\s*([\w+-]*)\s*$/;
  const RE_HEADING = /^(#{1,6})\s+(.*)$/;
  const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  const RE_QUOTE = /^\s*>\s?(.*)$/;
  const RE_LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const RE_TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function splitRow(line) {
    return line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
  }

  /** Build nested <ul>/<ol> from a run of list lines, by indent width. */
  function renderList(items, start = 0, indent = null) {
    const base = indent === null ? items[start].indent : indent;
    const ordered = /\d/.test(items[start].marker);
    let html = ordered ? '<ol>' : '<ul>';
    let i = start;

    while (i < items.length && items[i].indent >= base) {
      if (items[i].indent > base) {
        // Deeper items belong to the list item just emitted.
        const [nested, next] = renderList(items, i, items[i].indent);
        html = html.replace(/<\/li>$/, `${nested}</li>`);
        i = next;
        continue;
      }
      const task = /^\[([ xX])\]\s+(.*)$/.exec(items[i].text);
      const body = task
        ? `<label class="md-task"><input type="checkbox" disabled${
            task[1] === ' ' ? '' : ' checked'}> ${inline(task[2])}</label>`
        : inline(items[i].text);
      html += `<li>${body}</li>`;
      i += 1;
    }
    return [html + (ordered ? '</ol>' : '</ul>'), i];
  }

  function render(src) {
    const lines = String(src ?? '').split('\n');
    let html = '';
    let paragraph = [];
    let i = 0;

    const flush = () => {
      if (!paragraph.length) return;
      // Reflow soft-wrapped lines instead of preserving every newline.
      //
      // Agent output is hard-wrapped for an ~80-column terminal. Turning each
      // of those newlines into a <br> looks fine on a laptop and terrible on a
      // 390px phone: every line wraps naturally AND THEN breaks again wherever
      // the terminal happened to wrap, so sentences snap mid-phrase. Joining
      // with a space is also what CommonMark specifies.
      //
      // An explicit hard break — two trailing spaces or a trailing backslash,
      // which a writer has to mean — is still honoured.
      const parts = paragraph.map((line, idx) => {
        const hard = /(\s{2,}|\\)$/.test(line);
        const text = inline(line.replace(/(\s+|\\)$/, ''));
        const last = idx === paragraph.length - 1;
        if (last) return text;
        return hard ? `${text}<br>` : `${text} `;
      });
      html += `<p>${parts.join('')}</p>`;
      paragraph = [];
    };

    while (i < lines.length) {
      const line = lines[i];

      const fence = RE_FENCE.exec(line);
      if (fence) {
        flush();
        const lang = fence[1];
        const body = [];
        i += 1;
        while (i < lines.length && !RE_FENCE.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1; // consume the closing fence (or run off the end unterminated)
        html += `<pre class="md-pre"${lang ? ` data-lang="${escapeHtml(lang)}"` : ''}>`
          + `<code>${escapeHtml(body.join('\n'))}</code></pre>`;
        continue;
      }

      if (!line.trim()) { flush(); i += 1; continue; }

      if (RE_HR.test(line) && !RE_LIST.test(line)) {
        flush();
        html += '<hr>';
        i += 1;
        continue;
      }

      const heading = RE_HEADING.exec(line);
      if (heading) {
        flush();
        const level = Math.min(heading[1].length + 2, 6); // h1 -> h3, keep page hierarchy
        html += `<h${level}>${inline(heading[2])}</h${level}>`;
        i += 1;
        continue;
      }

      if (RE_QUOTE.test(line)) {
        flush();
        const body = [];
        while (i < lines.length && RE_QUOTE.test(lines[i])) {
          body.push(RE_QUOTE.exec(lines[i])[1]);
          i += 1;
        }
        html += `<blockquote>${render(body.join('\n'))}</blockquote>`;
        continue;
      }

      // A table needs its separator row directly under the header.
      if (line.includes('|') && i + 1 < lines.length && RE_TABLE_SEP.test(lines[i + 1])) {
        flush();
        const head = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        html += '<div class="md-table-wrap"><table><thead><tr>'
          + head.map((c) => `<th>${inline(c)}</th>`).join('')
          + '</tr></thead><tbody>'
          + rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')
          + '</tbody></table></div>';
        continue;
      }

      if (RE_LIST.test(line)) {
        flush();
        const items = [];
        while (i < lines.length && RE_LIST.test(lines[i])) {
          const m = RE_LIST.exec(lines[i]);
          items.push({ indent: m[1].length, marker: m[2], text: m[3] });
          i += 1;
        }
        html += renderList(items)[0];
        continue;
      }

      paragraph.push(line);
      i += 1;
    }

    flush();
    return html;
  }

  return { render, escapeHtml, safeUrl };
})();
