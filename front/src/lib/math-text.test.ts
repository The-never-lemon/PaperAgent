import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeMathSource, splitMathText } from "./math-text.ts";

test("多字母下标和 min() 会整理成 KaTeX 能排清楚的写法", () => {
  const source =
    "l_{rate} = d_{model}^{-0.5} \\cdot min(step_{num}^{-0.5}, step_{num} \\cdot warmup_{steps}^{-1.5})";
  const normalized = normalizeMathSource(source);

  assert.match(normalized, /\\min\(/);
  assert.doesNotMatch(normalized, /(?<!\\)min\(/);
  assert.match(normalized, /d_\{\\text\{model\}\}/);
  assert.match(normalized, /step_\{\\text\{num\}\}/);
  assert.match(normalized, /warmup_\{\\text\{steps\}\}/);
  assert.match(normalized, /l_\{\\text\{rate\}\}/);
});

test("没有花括号的 d_model、step_num 也会收成文字下标", () => {
  const normalized = normalizeMathSource("lrate = d_model^{-0.5} \\cdot min(step_num^{-0.5})");

  assert.match(normalized, /d_\{\\text\{model\}\}/);
  assert.match(normalized, /step_\{\\text\{num\}\}/);
  assert.match(normalized, /\\min\(/);
  assert.equal(normalized.includes("d_model"), false);
});

test("单字母下标 R_d、x_i 保持原样，避免把真下标收成单词", () => {
  assert.equal(normalizeMathSource("R_d"), "R_d");
  assert.equal(normalizeMathSource("x_i"), "x_i");
});

test("已经是 \\min、\\text 的公式不会被再包一层", () => {
  const source = "\\min(d_{\\text{model}}^{-0.5})";
  assert.equal(normalizeMathSource(source), source);
});

test("伪公式里的空格补救仍然有效", () => {
  const normalized = normalizeMathSource("MOM nM = X Pastn*21 days R_d");
  assert.equal(normalized.includes("\\ "), true);
  assert.equal(normalized.includes("R_d"), true);
});

test("报告字段里 $...$ 包住的学习率公式能被切出来", () => {
  const parts = splitMathText(
    "学习率按 $l_{rate} = d_{model}^{-0.5} \\cdot min(step_{num}^{-0.5})$ 调度",
  );
  assert.equal(parts.length, 3);
  assert.equal(parts[0]?.kind, "text");
  assert.equal(parts[1]?.kind, "math");
  if (parts[1]?.kind === "math") {
    assert.equal(parts[1].tex.includes("d_{model}"), true);
  }
});
