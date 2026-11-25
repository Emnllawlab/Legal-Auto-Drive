/* Local, document-scoped keyword matching. Offsets remain JavaScript UTF-16. */
(function (root) {
  'use strict';
  function normalize(text) {
    let value = '', starts = [], ends = [], offset = 0;
    for (const char of text) {
      const end = offset + char.length;
      for (const unit of char.normalize('NFKC').toLowerCase()) {
        if (/\s/u.test(unit)) continue;
        value += unit;
        for (let i = 0; i < unit.length; i++) { starts.push(offset); ends.push(end); }
      }
      offset = end;
    }
    return { value, starts, ends };
  }
  const key = s => `${s.start}:${s.end}:${s.label}`;
  const overlaps = (a, b) => a.start < b.end && b.start < a.end;
  const latin = c => /[\p{Script=Latin}\p{Number}_]/u.test(c || '');
  function match(text, terms, existing = [], excluded = new Set()) {
    const doc = normalize(text);
    const result = existing.map(s => ({ ...s }));
    let added = 0;
    const sorted = terms.map(t => ({ ...t, needle: normalize(t.text).value }))
      .filter(t => t.needle).sort((a, b) => b.needle.length - a.needle.length);
    for (const term of sorted) {
      let cursor = 0, at;
      while ((at = doc.value.indexOf(term.needle, cursor)) !== -1) {
        cursor = at + term.needle.length;
        const start = doc.starts[at], end = doc.ends[cursor - 1];
        // Do not turn Ann into an occurrence inside Joanne, or ID 12 into 123.
        const before = text[start - 1] || '';
        const after = text[end] || '';
        if ((latin(term.needle[0]) && latin(before)) ||
            (latin(term.needle.slice(-1)) && latin(after))) continue;
        const span = { start, end, text: text.slice(start, end), label: term.label,
          source: 'manual_keyword', termId: term.id, score: 1, low_conf: false };
        if (excluded.has(key(span))) continue;
        const hits = result.filter(s => overlaps(s, span));
        // Preserve explicit selections, longer matches and partial overlaps.
        if (hits.some(s => (s.start === start && s.end === end) ||
            s.source === 'manual' || s.start < start || s.end > end)) continue;
        for (const old of hits) result.splice(result.indexOf(old), 1);
        result.push(span);
        added++;
      }
    }
    return { spans: result.sort((a, b) => a.start - b.start), added };
  }
  const api = { normalize, key, match };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.DeskTerms = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
