const assert = require('node:assert/strict');
const { test } = require('node:test');
const { match, key } = require('../loci/static/desk_terms.js');
const term = (text, label = 'PERSON', id = text) => ({ text, label, id });

test('finds every occurrence across Chinese whitespace, line breaks and case variants', () => {
  const text = '华星科技 华 星\n科技\f华星科技\nAnn met ANN and Joanne. Acme\nLtd ACME LTD.';
  const result = match(text, [term('华星科技', 'COMPANY'), term('Ann'), term('Acme Ltd', 'COMPANY')]);
  assert.equal(result.added, 7);
  assert.equal(result.spans.filter(s => s.text.includes('Joanne')).length, 0);
  assert.equal(match(text, [term('Ann')], result.spans).added, 0);
});
test('literal punctuation and fullwidth characters are supported', () => {
  const text = 'A+B (测试) Ａ＋Ｂ (测试)';
  const result = match(text, [term('A+B'), term('(测试)')]);
  assert.equal(result.added, 4);
  result.spans.forEach(s => assert.equal(text.slice(s.start, s.end), s.text));
});
test('excluded occurrences stay excluded after another round', () => {
  const text = '张宁、张宁、张宁';
  const initial = match(text, [term('张宁')]).spans;
  const excluded = new Set([key(initial[1])]);
  const next = match(text, [term('张宁')], [initial[0], initial[2]], excluded);
  assert.equal(next.spans.length, 2);
  assert.equal(next.added, 0);
});
test('long terms replace contained automatic marks, without conflicting overlaps', () => {
  const text = '华星科技有限公司与华星科技';
  const existing = [{ start: 0, end: 4, text: '华星科技', label: 'COMPANY', source: 'rule' }];
  const result = match(text, [term('华星科技'), term('华星科技有限公司', 'COMPANY')], existing);
  assert.deepEqual(result.spans.map(s => [s.start, s.end]), [[0,8], [9,13]]);
  assert.equal(match(text, [term('华星科技有限公司')], [{...existing[0], source: 'manual'}]).added, 0);
});
test('UTF-16 offsets survive emoji and supplementary characters', () => {
  const text = '😀张宁\n𠮷田与张宁';
  const result = match(text, [term('张宁'), term('𠮷田')]);
  assert.deepEqual(result.spans.map(s => s.text), ['张宁', '𠮷田', '张宁']);
  result.spans.forEach(s => assert.equal(text.slice(s.start, s.end), s.text));
});
test('whitespace-only input and absent keywords produce no spans', () => {
  assert.equal(match('内容', [term(' \n'), term('不存在')]).added, 0);
});
