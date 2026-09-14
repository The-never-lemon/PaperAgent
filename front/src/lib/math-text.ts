/**
 * 把一段文字按数学公式拆开（精读报告字段渲染用）。
 *
 * 中文说明：
 * 模型写出来的报告字段里，公式是这种样子：
 *
 *     总损失为 $\mathcal{L}=\lambda_1\mathcal{L}_1+\lambda_2\mathcal{L}_2$。
 *
 * 直接当纯文本显示会看到一串反斜杠和花括号，没法读。所以先把它拆成
 * 「普通文字」和「公式」交替的几段，公式交给 KaTeX 排版，文字原样显示。
 *
 * 这里只负责拆，不负责画——画在 MathText.vue 里。
 */

/** 拆出来的一段：要么是普通文字，要么是一段公式。 */
export type MathPart =
  | { kind: "text"; value: string }
  | { kind: "math"; tex: string; display: boolean };

/**
 * 匹配一段公式。四种写法都支持，按顺序尝试：
 *
 *   $$...$$   独占一行的大公式（显示模式）
 *   \[...\]   同上，另一种常见写法
 *   \(...\)   行内公式
 *   $...$     行内公式
 *
 * 后两种（行内）的开头 $ 后面不能紧跟空白，结尾 $ 前面不能紧邻空白。
 * 这一条是专门用来躲开货币写法的：「价格 $5 到 $10」里的两个 $ 都不满足这个条件，
 * 所以整句会被当成普通文字，不会被误当成一段公式。
 *
 * 公式内部允许出现 `\$` 这样的转义写法，也允许换行（大公式常常占好几行）。
 */
const MATH_PATTERN =
  /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?![\s$])((?:\\.|[^$\\])+?)(?<![\s\\])\$/g;

/** 把一段文字拆成「普通文字 / 公式」交替的片段数组。 */
export function splitMathText(source: string): MathPart[] {
  const parts: MathPart[] = [];
  let cursor = 0;

  for (const match of source.matchAll(MATH_PATTERN)) {
    const index = match.index ?? 0;
    if (index > cursor) {
      parts.push({ kind: "text", value: source.slice(cursor, index) });
    }

    // 四个捕获组对应四种写法，命中的那个组一定有值。
    const tex = match[1] ?? match[2] ?? match[3] ?? match[4] ?? "";
    parts.push({
      kind: "math",
      tex: tex.trim(),
      // 前两种（$$ 与 \[ \]）是独占一行的大公式，后两种是行内公式。
      display: match[1] !== undefined || match[2] !== undefined,
    });

    cursor = index + match[0].length;
  }

  if (cursor < source.length) {
    parts.push({ kind: "text", value: source.slice(cursor) });
  }

  // 一个公式都没匹配到时，整段当作普通文字返回，免得上层拿到空数组。
  return parts.length > 0 ? parts : [{ kind: "text", value: source }];
}


/**
 * 判断公式源码是不是「真 LaTeX」。
 *
 * 中文说明：判据只有一条——有没有反斜杠命令（`\` 后面跟字母，比如 \mathcal、\frac、
 * \sum），或者双反斜杠 `\\`（LaTeX 里表示换行）。有就是真 LaTeX，没有就是拿 Unicode
 * 符号和英文字母拼出来的「伪公式」。
 *
 * 为什么要分这两类：KaTeX 走的是数学模式，而数学模式会**把空格全部吃掉**。真 LaTeX
 * 里的空格只是写给人看的排版缩进，吃掉没关系（它靠 \cdot、\times 这类命令表示间隔）；
 * 但伪公式是靠空格分词的，吃掉就粘成一坨没法读。所以下面那点补救只对伪公式做。
 */
const LATEX_COMMAND_PATTERN = /\\[A-Za-z]|\\\\/;

/**
 * 多字母函数名。写成 min( 而不是 \min( 时，KaTeX 会把 m、i、n 当成三个斜体变量
 * 乘在一起，截图里那种「斜体 min」就是这么来的。按从长到短排，避免 cos 抢在 arccos 前面。
 */
const MATH_FUNCTION_NAMES = [
  "arcsin",
  "arccos",
  "arctan",
  "sinh",
  "cosh",
  "tanh",
  "min",
  "max",
  "log",
  "exp",
  "sin",
  "cos",
  "tan",
  "lim",
  "inf",
  "sup",
  "arg",
  "det",
  "ker",
  "dim",
];

const MATH_FUNCTION_PATTERN = new RegExp(
  `(?<![A-Za-z\\\\])(${MATH_FUNCTION_NAMES.join("|")})\\s*\\(`,
  "g",
);

/**
 * 没有花括号的多字母下标：d_model、step_num。
 * 单字母下标（R_d、x_i）故意不匹配——那是真的数学下标。
 */
const SNAKE_SUBSCRIPT_PATTERN = /(?<![\\A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)_([A-Za-z][A-Za-z0-9]+)(?![A-Za-z0-9{])/g;

/**
 * 已经有花括号、但是里面是一整段英文单词的下标：_{model}、_{rate}。
 * 不配 \text / \mathrm 这类命令，也不配 _{i} 这种单字母。
 */
const BRACED_WORD_SUBSCRIPT_PATTERN = /_\{([A-Za-z][A-Za-z0-9\-]{1,})\}/g;

/**
 * 把「伪公式」里的空白换成 KaTeX 能看见的空格，并把报告里常见的
 * min( / d_model / _{model} 整理成 KaTeX 能排清楚的写法。
 *
 * 中文说明：
 * 数学模式不认空格，`$MOM nM = X Pastn*21 days R_d$` 会被排成
 * `MOMnM = XPastn∗21daysR_d`——空格全没了、单词粘在一起，读不出来。
 *
 * 补救办法是把每一段空白换成 KaTeX 的显式空格命令 `\ `（反斜杠加一个空格），它在数学
 * 模式下会照样排出一个空格，于是上面那句变成：
 *
 *     MOM nM = X Pastn ∗ 21 days R_d
 *
 * 短符号（$R_d$、$x1$）本来就没有空白，换了等于没换，下标上标照常生效，所以这条补救
 * 伤不到它们。
 *
 * 反过来，**源码里只要有反斜杠命令就不要再改空格**：真 LaTeX 自己管间距，硬塞显式
 * 空格只会让它变松散（`\mathcal{L} = \lambda_1 \mathcal{L}_1` 会排成 `L  =  λ₁ L₁`）。
 *
 * 但「有反斜杠」不等于「下标已经写对了」。模型经常写出
 * `$l_{rate}=d_{model}^{-0.5}\cdot min(step_{num}^{-0.5})$` 这种半成品：
 * `\cdot` 是真命令，`min` 和 `_{model}` 却还是斜体变量。所以标识符整理必须在
 * 「要不要改空格」之前做，真假公式都走一遍。
 */
export function normalizeMathSource(source: string): string {
  const tex = protectMathIdentifiers(source);
  if (LATEX_COMMAND_PATTERN.test(tex)) {
    return tex;
  }
  // 连续空白合成一个显式空格：多个空格和换行在数学模式里本来也只算一个分隔。
  return tex.replace(/[ \t\r\n]+/g, "\\ ");
}

/**
 * 把报告里常见的半成品标识符收成 KaTeX 能读的形式。
 *
 * 中文说明：
 * 1. min( → \min(，否则三个斜体字母乘在一起；
 * 2. d_model → d_{\text{model}}，否则只有 m 是下标、odel 落在外面；
 * 3. _{model} → _{\text{model}}，否则下标里的单词被当成一串斜体变量。
 */
function protectMathIdentifiers(source: string): string {
  MATH_FUNCTION_PATTERN.lastIndex = 0;
  SNAKE_SUBSCRIPT_PATTERN.lastIndex = 0;
  BRACED_WORD_SUBSCRIPT_PATTERN.lastIndex = 0;
  let tex = source.replace(MATH_FUNCTION_PATTERN, "\\$1(");
  tex = tex.replace(SNAKE_SUBSCRIPT_PATTERN, "$1_{\\text{$2}}");
  tex = tex.replace(BRACED_WORD_SUBSCRIPT_PATTERN, "_{\\text{$1}}");
  return tex;
}

/**
 * 判断一段文字是不是「其实是一条公式」（用来救那些被反引号包住的公式）。
 *
 * 中文说明：
 * 模型很爱把公式写成行内代码，比如 `B_i,t+1 = B_i,t − ‖x_i,t+1 − x_i,t‖₂`。前端照
 * Markdown 的规矩把它渲染成灰色等宽代码块——可那根本不是代码，是公式，用户要的是
 * 把它排版出来。这个函数就是用来认出这种情况的。
 *
 * 判据只用「Unicode 数学符号」：希腊字母、数学运算符（∑ √ ≤ ≥ ∈ ∉ −）、上下标、
 * 箭头、数学字母数字，另外补上 · × ÷ ± ‖ 这几个散落在别的区段的符号。
 *
 * ## 为什么只认这些字符
 *
 * 行内代码里绝大多数是**真代码**：命令、路径、正则、标识符。上面这些字符一个都不可能
 * 出现在代码里，所以拿它们当判据，误伤面几乎为零。实测把所有历史会话消息与精读产物
 * 扫了一遍，「行内代码含 Unicode 数学符号」共 9 处，9 处全是公式，没有一处是代码。
 *
 * 刻意不认的三类：
 *   - ASCII 的 `=` `-` `|` `*` 这类符号——真代码里到处都是（`docker run -it`、`a|b`、
 *     `x = 1`），认了就会把大段代码误判成公式。
 *   - 反斜杠——正则和转义序列天天用（`\d+\w*`），认了会被当成 LaTeX 命令送进 KaTeX，
 *     然后整段渲染成一行红字报错。
 *   - 中文——代码里也可能出现中文；而且中文本身说明不了什么，不靠它判断。
 */
// 下面这个字符类按顺序覆盖：希腊字母 U+0370–03FF、修饰字母 U+1D00–1D7F（含 ᵀ）、上下标
// U+2070–209F、箭头 U+2190–21FF、数学运算符 U+2200–22FF、补充数学运算符 U+2A00–2AFF、
// 数学字母数字 U+1D400–1D7FF（𝐀 这一类，在增补平面，所以必须带 u 标志才认得对），
// 最后几个（± · × ÷ ‖）散落在别的区段，单独补进来。
const MATH_SYMBOL_PATTERN =
  /[Ͱ-Ͽᵀ-ᵿ⁰-₟←-⇿∀-⋿⨀-⫿±·×÷‖𝐀-𝟿]/u;

export function looksLikeFormula(source: string): boolean {
  return MATH_SYMBOL_PATTERN.test(source);
}


/**
 * 剥掉文字里的「裸 LaTeX 标记」（论文摘要里常见）。
 *
 * 中文说明：
 * 检索回来的论文摘要经常自带 LaTeX 标记，而且**不带 $ 定界符**，例如：
 *
 *     The \textit{PIMAEX} reward, short for Peer-Incentivized Multi-Agent…
 *     …achieves 88.5\% of the baseline…
 *
 * 这种文字直接显示就是一堆反斜杠。带 $ 定界符的公式归 splitMathText 管，
 * 这个函数专门收拾剩下那些没有定界符的标记。
 *
 * 它**自己会保护带定界符的公式**：碰到 $...$ 这类整段原样搬走，一个字都不动。
 * 所以它可以直接用在原始文本上，不需要调用方先把公式摘出去。这条是必须的——
 * 公式里 \alpha、\% 到处都是，要是被当成裸标记剥掉，公式就毁了。
 *
 * ## 判据：闭集白名单，不是「反斜杠加英文单词」
 *
 * 只认下面三类，认不出的一律原样保留：
 *
 *   1. 文字样式命令：\textit{...} \textbf{...} \emph{...} 等（见 TEXT_COMMANDS），
 *      剥掉外壳只留里面的字。花括号必须配平、不能跨行。
 *   2. 链接命令：\href{网址}{显示文字} 留显示文字；\url{网址} 留网址。
 *   3. 转义还原：\% \& \_ \# 还原成字面字符；句点后的「反斜杠+空格」还原成一个空格
 *      （LaTeX 里它表示句子结束后的正常间距，所以 `vs.\ exploitation` 要变成 `vs. exploitation`）。
 *
 * ## 为什么必须用白名单
 *
 * 如果判据放宽成「反斜杠加一个英文单词」，下面这些正常文本会被改坏，而它们在本项目里
 * 真实存在（这是 Windows 上的项目，目录树里就有 src/models）：
 *
 *     C:\Users\Admin                      ->  \Users 不是命令，原样保留
 *     D:\Java_Project\...\src\models      ->  \src \models 都不是命令
 *     正则 \d+\w*\s                       ->  \d \w \s 都不是命令
 *     \d{2,4}                             ->  就算带花括号，也不在白名单里
 *     \Users{Admin}                       ->  同样不在白名单
 *     路径以 \ 作为分隔符                  ->  反斜杠后面是空格，且前面不是句点
 *
 * ## 刻意不做的两件事
 *
 * - **不认裸数学命令**（\alpha、\lambda、\times、\frac 这种没被 $ 包起来的）。
 *   实测本项目 143 篇论文的元数据里，这类写法出现 0 次——收益为零，而误伤风险
 *   全部来自放宽白名单。所以宁可让裸 \alpha 保持原样（改之前本来也是原样）。
 * - **不还原 \{ \} \$ 和 \***。前三个还原会改坏 `C:\Users\{username}\AppData`、
 *   `C:\$Recycle.Bin` 这类路径；`\*` 在本项目的 Markdown 里表示斜体，还原会凭空造出斜体。
 */
export function stripBareLatex(source: string): string {
  // 绝大多数文本里没有反斜杠，直接返回，不做任何扫描。
  if (!source || !source.includes("\\")) {
    return source;
  }

  let out = "";
  let index = 0;
  while (index < source.length) {
    const char = source[index];

    // 先看这里是不是一个完整的公式（$...$ 这类）。是就整段原样搬走，一个字都不动——
    // 公式里的 \alpha、\% 归 KaTeX 解释，不能在这里被当成裸标记剥掉。
    // 这一步让函数可以直接用在原始文本上（后端清洗整段摘要时会这么用），
    // 而不是必须由调用方先把公式摘出去。
    if (char === "$" || char === "\\") {
      const math = readDelimitedMath(source, index);
      if (math) {
        out += math;
        index += math.length;
        continue;
      }
    }

    if (char !== "\\") {
      out += char;
      index += 1;
      continue;
    }

    const href = readHrefCommand(source, index);
    if (href) {
      out += href.text;
      index = href.end;
      continue;
    }

    const url = readUrlCommand(source, index);
    if (url) {
      out += url.text;
      index = url.end;
      continue;
    }

    const styled = readTextCommand(source, index);
    if (styled) {
      // 内容里可能还套着别的命令（\textbf{\textit{X}}），递归再剥一遍。
      out += stripBareLatex(styled.content);
      index = styled.end;
      continue;
    }

    const escaped = readEscape(source, index);
    if (escaped) {
      out += escaped.text;
      index = escaped.end;
      continue;
    }

    // 认不出来：这个反斜杠原样保留，继续往后看。
    out += char;
    index += 1;
  }
  return out;
}

/**
 * 判断 index 处是不是一个以定界符包起来的公式。
 * 是就返回整段原文（连定界符一起），不是返回 null。
 *
 * 中文说明：这四条规则和上面 splitMathText 用的 MATH_PATTERN 完全一致，
 * 只是这里要求「刚好从 index 开始」，所以写成带 ^ 的独立正则。
 */
function readDelimitedMath(source: string, index: number): string | null {
  const rest = source.slice(index);
  for (const pattern of DELIMITED_MATH_AT) {
    const match = pattern.exec(rest);
    if (match) {
      return match[0];
    }
  }
  return null;
}

/** 依次尝试：$$…$$、\[…\]、\(…\)、$…$。顺序不能换，$$ 必须优先于 $。 */
const DELIMITED_MATH_AT = [
  /^\$\$[\s\S]+?\$\$/,
  /^\\\[[\s\S]+?\\\]/,
  /^\\\([\s\S]+?\\\)/,
  /^\$(?![\s$])(?:\\.|[^$\\])+?(?<![\s\\])\$/,
];

/** 允许被剥壳的文字样式命令。按长度从长到短排，避免 \text 抢在 \textit 前面匹配。 */
const TEXT_COMMANDS = [
  "textnormal",
  "textit",
  "textbf",
  "texttt",
  "textrm",
  "textsf",
  "textsc",
  "mbox",
  "text",
  "emph",
];

/** 单个命令括号里最多允许多少字（超过就认为不是一条样式命令，原样保留）。 */
const MAX_COMMAND_ARG_CHARS = 200;

/** 从 start 处的反斜杠开始，尝试读一个 \href{网址}{显示文字}。读不出返回 null。 */
function readHrefCommand(source: string, start: number): { text: string; end: number } | null {
  if (!source.startsWith("\\href", start)) {
    return null;
  }
  const target = readBracedGroup(source, skipSpaces(source, start + "\\href".length));
  if (!target) {
    return null;
  }
  const labelStart = skipSpaces(source, target.end);
  const label = readBracedGroup(source, labelStart);
  if (!label) {
    return null;
  }
  // 有显示文字就用显示文字，没有就退回网址本身。
  return { text: label.content || target.content, end: label.end };
}

/** 从 start 处尝试读一个 \url{网址}。读不出返回 null。 */
function readUrlCommand(source: string, start: number): { text: string; end: number } | null {
  if (!source.startsWith("\\url", start)) {
    return null;
  }
  const group = readBracedGroup(source, skipSpaces(source, start + "\\url".length));
  if (!group) {
    return null;
  }
  return { text: group.content, end: group.end };
}

/**
 * 从 start 处尝试读一个白名单里的文字样式命令（如 \textit{内容}）。
 *
 * 中文说明：三个条件同时满足才算数——命令在白名单里、后面紧跟一对配平的花括号、
 * 括号内容不跨行且不太长。任一条不满足就返回 null，让上层原样保留这段文字。
 */
function readTextCommand(source: string, start: number): { content: string; end: number } | null {
  for (const command of TEXT_COMMANDS) {
    const token = "\\" + command;
    if (!source.startsWith(token, start)) {
      continue;
    }
    // 避免把 \textitX 这种「命令名后面还粘着字母」的写法误认成 \textit。
    const next = source[start + token.length];
    if (next !== undefined && /[A-Za-z]/.test(next)) {
      return null;
    }
    const group = readBracedGroup(source, skipSpaces(source, start + token.length));
    if (!group) {
      return null;
    }
    if (group.content.includes("\n") || group.content.length > MAX_COMMAND_ARG_CHARS) {
      return null;
    }
    return group;
  }
  return null;
}

/** 允许还原成字面字符的转义（刻意只有这四个，不含 { } $）。 */
const ESCAPED_CHARACTERS = "%&_#";

/** 从 start 处尝试还原一个转义字符。读不出返回 null。 */
function readEscape(source: string, start: number): { text: string; end: number } | null {
  const next = source[start + 1];
  if (next !== undefined && ESCAPED_CHARACTERS.includes(next)) {
    return { text: next, end: start + 2 };
  }
  // 句点后面的「反斜杠 + 空格」：LaTeX 用它表示句子结束后的正常间距。
  if (next === " " && source[start - 1] === ".") {
    return { text: " ", end: start + 2 };
  }
  return null;
}

/** 跳过若干空格，返回第一个非空格字符的位置。 */
function skipSpaces(source: string, start: number): number {
  let index = start;
  while (source[index] === " ") {
    index += 1;
  }
  return index;
}

/** 读一对花括号包起来的内容，返回内容和结束位置。括号不配平就返回 null。 */
function readBracedGroup(source: string, start: number): { content: string; end: number } | null {
  if (source[start] !== "{") {
    return null;
  }
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (char === "\\") {
      // 跳过被转义的下一个字符，免得把 \{ 当成一层新括号。
      index += 1;
      continue;
    }
    if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) {
        return { content: source.slice(start + 1, index), end: index + 1 };
      }
    }
  }
  return null;
}
