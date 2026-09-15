<script setup lang="ts">
/**
 * 轻量 Markdown 渲染组件（专为聊天气泡设计）。
 *
 * 中文说明：
 * 大模型的回复天然是 Markdown（加粗、列表、代码块、链接……），直接按纯文本
 * 显示会出现满屏星号，体验很差。这个组件把 Markdown 解析成结构化块后用
 * Vue 模板渲染——全程不使用 v-html，模型输出里就算夹带 HTML 也只会当成
 * 普通文字显示，没有任何注入风险；也不引入第三方渲染库。
 *
 * 支持的语法：标题(#)、无序/有序列表、引用(>)、围栏代码块(```)、分隔线(---)、
 * 简单表格(|…|)、段落，以及行内的 **加粗**、*斜体*、`代码`、[链接](http…)、
 * 数学公式（$...$、$$...$$、\(...\)、\[...\]）。
 *
 * 有一处偏离标准 Markdown 的约定：**行内代码里若含 Unicode 数学符号，按公式渲染**
 * （例如 `B_i,t+1 = B_i,t − ‖x‖₂`）。模型习惯把公式写成行内代码，而代码块的样子并不
 * 适合读公式。判据与理由见 lib/math-text.ts 的 looksLikeFormula，真代码不受影响。
 *
 * 流式容错：模型逐字输出时语法常常"写到一半"（``` 还没闭合、** 只有一边），
 * 解析器对未闭合结构一律按"直到结尾"处理，成对语法匹配不上就原样显示，
 * 因此流式过程中画面不会闪烁错乱。公式的定界符没写全时也按同样的思路处理：
 * 切不出公式就整段当普通文字。
 */
import { computed } from "vue";

import { looksLikeFormula, splitMathText, stripBareLatex } from "../../lib/math-text";
import { resolvePaperRef } from "../../lib/paper-link";
import MathFormula from "./MathFormula.vue";

defineOptions({ name: "MarkdownText" });

const props = defineProps<{
  content: string;
  /** 流式输出中：在内容末尾显示闪烁光标。 */
  streaming?: boolean;
  /** 工作区论文编号对照表：DOI / arXiv 等写法都能对上主键。 */
  paperRefLookup?: Map<string, string>;
  /** 工作区里真实存在的 paper_id 集合。[xxx] 形式的引用如果匹配就渲染成可点击按钮。 */
  knownPaperIds?: Set<string>;
}>();

const emit = defineEmits<{
  /** 用户点击了一个 paper_id 引用。 */
  paperClick: [paperId: string];
}>();

/** 行内片段：普通文字 / 加粗 / 斜体 / 行内代码 / 链接 / paper_id 引用 / 公式。 */
interface InlinePart {
  text: string;
  bold?: boolean;
  italic?: boolean;
  code?: boolean;
  href?: string;
  /** 如果是 paper_id 引用，这里存编号。 */
  paperId?: string;
  /** 如果是公式，这段 text 存的是公式源码（不含 $ 定界符）。 */
  math?: boolean;
  /** 公式是否独占一行（$$...$$ 这种写法）。 */
  display?: boolean;
}

/** 块级结构：段落、标题、列表、引用、代码块、分隔线、表格。 */
type Block =
  | { type: "paragraph"; lines: InlinePart[][] }
  | { type: "heading"; level: number; parts: InlinePart[] }
  | { type: "list"; ordered: boolean; items: InlinePart[][] }
  | { type: "quote"; lines: InlinePart[][] }
  | { type: "code"; lang: string; code: string }
  | { type: "hr" }
  | { type: "table"; head: InlinePart[][]; rows: InlinePart[][][] };

/** 行内语法匹配：`代码`、***粗斜***、**加粗**、*斜体*、[文字](链接)、[paper_id]。 */
const INLINE_PATTERN = /(`[^`\n]+`)|(\*\*\*[^*\n]+\*\*\*)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]+\]\((?:https?:\/\/|\/)[^)\s]*\))|(\[[a-zA-Z0-9_\-./:]+\])/g;

/**
 * 把一行文字解析成行内片段列表；匹配不上的部分原样保留。
 *
 * 中文说明：公式要最先切出来。原因是 Markdown 的行内规则里 `*` 表示斜体、
 * `[x]` 可能被当成论文编号去渲染成按钮，而公式里这些符号到处都是——比如
 * $a*b*c$ 里的 *b* 会被当成斜体切走，公式就被拆坏了。所以先把公式整段摘出来，
 * 剩下的纯文字再交给 Markdown 的行内规则处理。
 */
function parseInline(text: string): InlinePart[] {
  const parts: InlinePart[] = [];
  for (const segment of splitMathText(text)) {
    if (segment.kind === "math") {
      parts.push({ text: segment.tex, math: true, display: segment.display });
    } else {
      parts.push(...parseMarkdownInline(segment.value));
    }
  }
  return parts;
}

/** 解析不含公式的那部分文字（加粗 / 斜体 / 行内代码 / 链接 / paper_id 引用）。 */
function parseMarkdownInline(text: string): InlinePart[] {
  const parts: InlinePart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(INLINE_PATTERN)) {
    const index = match.index ?? 0;
    if (index > cursor) {
      // 中文说明：只对「没被行内语法匹配走」的纯文字剥裸 LaTeX 标记。
      // 放在这里而不是函数开头，是因为行内代码要先被摘成独立片段——
      // 代码里的 \textit 必须逐字保留，不能当标记剥掉。
      parts.push({ text: stripBareLatex(text.slice(cursor, index)) });
    }
    const raw = match[0];
    if (raw.startsWith("`")) {
      const inner = raw.slice(1, -1);
      // 中文注释：模型很爱把公式写成行内代码，例如 `B_i,t+1 = B_i,t − ‖x_i,t+1 − x_i,t‖₂`。
      // 照 Markdown 的规矩这该渲染成灰色代码块，可它根本不是代码而是公式，代码块的样子
      // 用户读着别扭。所以内容里出现 Unicode 数学符号时，改走公式渲染那条路。
      // 判据只看 Unicode 数学符号（见 math-text.ts 的 looksLikeFormula），真代码
      // ——命令、路径、正则、标识符——一个都命中不了，不会被误伤。
      if (looksLikeFormula(inner)) {
        parts.push({ text: inner, math: true, display: false });
      } else {
        parts.push({ text: inner, code: true });
      }
    } else if (raw.startsWith("***")) {
      parts.push({ text: raw.slice(3, -3), bold: true, italic: true });
    } else if (raw.startsWith("**")) {
      parts.push({ text: raw.slice(2, -2), bold: true });
    } else if (raw.startsWith("*")) {
      parts.push({ text: raw.slice(1, -1), italic: true });
    } else if (raw.includes("](")) {
      // 链接：拆出显示文字和地址，地址只做展示级校验（http/相对路径）。
      const splitAt = raw.indexOf("](");
      const label = raw.slice(1, splitAt);
      const href = raw.slice(splitAt + 2, -1);
      parts.push({ text: label, href });
    } else if (raw.startsWith("[") && raw.endsWith("]")) {
      // 可能是论文编号：主键对得上，或 DOI / arXiv 写法能对上工作区里的某篇，就做成按钮。
      const candidate = raw.slice(1, -1);
      const resolved = resolvePaperRef(candidate, props.paperRefLookup);
      if (resolved) {
        parts.push({ text: candidate, paperId: resolved });
      } else if (props.knownPaperIds?.has(candidate)) {
        parts.push({ text: candidate, paperId: candidate });
      } else {
        parts.push({ text: raw });
      }
    }
    cursor = index + raw.length;
  }
  if (cursor < text.length) {
    parts.push({ text: stripBareLatex(text.slice(cursor)) });
  }
  return parts.length > 0 ? parts : [{ text: stripBareLatex(text) }];
}

/** 判断一行是不是列表项，并返回是否有序。 */
function listMarker(line: string): { ordered: boolean; text: string } | null {
  const match = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
  if (!match) {
    return null;
  }
  return { ordered: Boolean(match[2]), text: match[3] };
}

/** 把表格行 "| a | b |" 拆成单元格数组。 */
function splitTableRow(line: string): string[] {
  return line
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/** 判断是不是表格第二行的分隔行（|---|---|）。 */
function isTableDivider(line: string): boolean {
  return /^\s*\|?\s*:?-{2,}.*\|/.test(line) && line.includes("-");
}

/** 主解析：把整段 Markdown 文本切成块级结构。 */
function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // 空行：块之间的分隔，直接跳过。
    if (!line.trim()) {
      i += 1;
      continue;
    }

    // 围栏代码块：收集到闭合围栏为止；没闭合就收到结尾（流式容错）。
    if (/^\s*```/.test(line)) {
      const lang = line.trim().slice(3).trim();
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过闭合围栏（如果存在）
      blocks.push({ type: "code", lang, code: codeLines.join("\n") });
      continue;
    }

    // 标题：# ~ ######。
    const heading = /^\s*(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, parts: parseInline(heading[2]) });
      i += 1;
      continue;
    }

    // 分隔线。
    if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    // 引用块：连续 > 行。
    if (/^\s*>\s?/.test(line)) {
      const quoteLines: InlinePart[][] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoteLines.push(parseInline(lines[i].replace(/^\s*>\s?/, "")));
        i += 1;
      }
      blocks.push({ type: "quote", lines: quoteLines });
      continue;
    }

    // 表格：当前行含 |，且下一行是分隔行。
    if (line.includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
      const head = splitTableRow(line).map(parseInline);
      i += 2;
      const rows: InlinePart[][][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]).map(parseInline));
        i += 1;
      }
      blocks.push({ type: "table", head, rows });
      continue;
    }

    // 列表：连续的列表项行（有序/无序各自成组，以第一项的类型为准）。
    const firstItem = listMarker(line);
    if (firstItem) {
      const items: InlinePart[][] = [];
      const ordered = firstItem.ordered;
      while (i < lines.length) {
        const marker = listMarker(lines[i]);
        if (!marker) {
          break;
        }
        // 有序和无序混排时以第一项为准，后面的仍收进同一列表。
        items.push(parseInline(marker.text));
        i += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // 普通段落：收集连续的非空、非特殊行。
    const paragraphLines: InlinePart[][] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*```/.test(lines[i]) &&
      !/^\s*#{1,6}\s/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !listMarker(lines[i]) &&
      !/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(lines[i])
    ) {
      paragraphLines.push(parseInline(lines[i]));
      i += 1;
    }
    if (paragraphLines.length > 0) {
      blocks.push({ type: "paragraph", lines: paragraphLines });
    } else {
      // 理论上到不了这里；兜底前进一行，避免死循环。
      i += 1;
    }
  }

  return blocks;
}

const blocks = computed<Block[]>(() => {
  // 中文说明：编号对照表变了也要重新切一遍，否则刚检索进来的论文点不了。
  void props.paperRefLookup;
  void props.knownPaperIds;
  return parseBlocks(props.content ?? "");
});

/** 最后一个块是不是段落/列表（决定光标贴在哪里显示）。 */
const lastBlockIndex = computed(() => blocks.value.length - 1);
</script>

<template>
  <div class="chat-md">
    <template v-for="(block, index) in blocks" :key="index">
      <!-- 代码块：保留等宽与换行，左上角标出语言 -->
      <div v-if="block.type === 'code'" class="chat-md-code-wrap">
        <span v-if="block.lang" class="chat-md-code-lang">{{ block.lang }}</span>
        <pre class="chat-md-code">{{ block.code }}</pre>
      </div>

      <!-- 标题 -->
      <component
        :is="`h${Math.min(6, Math.max(3, block.level + 2))}`"
        v-else-if="block.type === 'heading'"
        class="chat-md-heading"
      >
        <template v-for="(part, partIndex) in block.parts" :key="partIndex">
          <MathFormula v-if="part.math" :tex="part.text" :display="part.display" />
          <code v-else-if="part.code" class="chat-md-inline-code">{{ part.text }}</code>
          <a v-else-if="part.href" :href="part.href" target="_blank" rel="noopener noreferrer">{{ part.text }}</a>
          <strong v-else-if="part.bold && part.italic"><em>{{ part.text }}</em></strong>
          <strong v-else-if="part.bold">{{ part.text }}</strong>
          <em v-else-if="part.italic">{{ part.text }}</em>
          <button
            v-else-if="part.paperId"
            type="button"
            class="chat-md-paper-ref"
            @click="emit('paperClick', part.paperId!)"
          >[{{ part.text }}]</button>
          <template v-else>{{ part.text }}</template>
        </template>
      </component>

      <!-- 列表 -->
      <component :is="block.ordered ? 'ol' : 'ul'" v-else-if="block.type === 'list'" class="chat-md-list">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <template v-for="(part, partIndex) in item" :key="partIndex">
            <MathFormula v-if="part.math" :tex="part.text" :display="part.display" />
            <code v-else-if="part.code" class="chat-md-inline-code">{{ part.text }}</code>
            <a v-else-if="part.href" :href="part.href" target="_blank" rel="noopener noreferrer">{{ part.text }}</a>
            <strong v-else-if="part.bold && part.italic"><em>{{ part.text }}</em></strong>
            <strong v-else-if="part.bold">{{ part.text }}</strong>
            <em v-else-if="part.italic">{{ part.text }}</em>
            <button
              v-else-if="part.paperId"
              type="button"
              class="chat-md-paper-ref"
              @click="emit('paperClick', part.paperId!)"
            >[{{ part.text }}]</button>
            <template v-else>{{ part.text }}</template>
          </template>
          <span
            v-if="streaming && index === lastBlockIndex && itemIndex === block.items.length - 1"
            class="chat-cursor"
            aria-hidden="true"
          ></span>
        </li>
      </component>

      <!-- 引用 -->
      <blockquote v-else-if="block.type === 'quote'" class="chat-md-quote">
        <p v-for="(quoteLine, lineIndex) in block.lines" :key="lineIndex">
          <template v-for="(part, partIndex) in quoteLine" :key="partIndex">
            <MathFormula v-if="part.math" :tex="part.text" :display="part.display" />
            <code v-else-if="part.code" class="chat-md-inline-code">{{ part.text }}</code>
            <a v-else-if="part.href" :href="part.href" target="_blank" rel="noopener noreferrer">{{ part.text }}</a>
            <strong v-else-if="part.bold && part.italic"><em>{{ part.text }}</em></strong>
            <strong v-else-if="part.bold">{{ part.text }}</strong>
            <em v-else-if="part.italic">{{ part.text }}</em>
            <template v-else>{{ part.text }}</template>
          </template>
        </p>
      </blockquote>

      <!-- 表格 -->
      <div v-else-if="block.type === 'table'" class="chat-md-table-wrap">
        <table class="chat-md-table">
          <thead>
            <tr>
              <th v-for="(cell, cellIndex) in block.head" :key="cellIndex">
                <template v-for="(part, partIndex) in cell" :key="partIndex">
                  <MathFormula v-if="part.math" :tex="part.text" :display="part.display" />
                  <code v-else-if="part.code" class="chat-md-inline-code">{{ part.text }}</code>
                  <a v-else-if="part.href" :href="part.href" target="_blank" rel="noopener noreferrer">{{ part.text }}</a>
                  <strong v-else-if="part.bold && part.italic"><em>{{ part.text }}</em></strong>
                  <strong v-else-if="part.bold">{{ part.text }}</strong>
                  <em v-else-if="part.italic">{{ part.text }}</em>
                  <button
                    v-else-if="part.paperId"
                    type="button"
                    class="chat-md-paper-ref"
                    @click="emit('paperClick', part.paperId!)"
                  >[{{ part.text }}]</button>
                  <template v-else>{{ part.text }}</template>
                </template>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
              <td v-for="(cell, cellIndex) in row" :key="cellIndex">
                <template v-for="(part, partIndex) in cell" :key="partIndex">
                  <MathFormula v-if="part.math" :tex="part.text" :display="part.display" />
                  <code v-else-if="part.code" class="chat-md-inline-code">{{ part.text }}</code>
                  <a v-else-if="part.href" :href="part.href" target="_blank" rel="noopener noreferrer">{{ part.text }}</a>
                  <strong v-else-if="part.bold && part.italic"><em>{{ part.text }}</em></strong>
                  <strong v-else-if="part.bold">{{ part.text }}</strong>
                  <em v-else-if="part.italic">{{ part.text }}</em>
                  <!-- 中文注释：表格里的 [paper_id] 引用也要渲染成可点按钮。
                       之前只有段落/列表/标题三个分支做了这件事，表格漏了，
                       结果模型一旦把论文清单放进表格，引用就退化成纯文本、点不动。 -->
                  <button
                    v-else-if="part.paperId"
                    type="button"
                    class="chat-md-paper-ref"
                    @click="emit('paperClick', part.paperId!)"
                  >[{{ part.text }}]</button>
                  <template v-else>{{ part.text }}</template>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 分隔线 -->
      <hr v-else-if="block.type === 'hr'" class="chat-md-hr" />

      <!-- 段落 -->
      <p v-else class="chat-md-paragraph">
        <template v-for="(paragraphLine, lineIndex) in block.lines" :key="lineIndex">
          <br v-if="lineIndex > 0" />
          <template v-for="(part, partIndex) in paragraphLine" :key="partIndex">
            <MathFormula v-if="part.math" :tex="part.text" :display="part.display" />
            <code v-else-if="part.code" class="chat-md-inline-code">{{ part.text }}</code>
            <a v-else-if="part.href" :href="part.href" target="_blank" rel="noopener noreferrer">{{ part.text }}</a>
            <strong v-else-if="part.bold && part.italic"><em>{{ part.text }}</em></strong>
            <strong v-else-if="part.bold">{{ part.text }}</strong>
            <em v-else-if="part.italic">{{ part.text }}</em>
            <button
              v-else-if="part.paperId"
              type="button"
              class="chat-md-paper-ref"
              @click="emit('paperClick', part.paperId!)"
            >[{{ part.text }}]</button>
            <template v-else>{{ part.text }}</template>
          </template>
          <span
            v-if="streaming && index === lastBlockIndex && lineIndex === block.lines.length - 1"
            class="chat-cursor"
            aria-hidden="true"
          ></span>
        </template>
      </p>
    </template>
  </div>
</template>
